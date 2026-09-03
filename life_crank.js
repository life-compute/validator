#!/usr/bin/env node
/**
 * LIFE Compute — Permissionless mint_reward crank daemon.
 *
 * Two-source processing:
 *   1. Queue file  (CRANK_QUEUE_FILE, default: output/crank_queue.jsonl)
 *      The validator daemon appends one JSON line per CONFIRM+tx landing.
 *      Lines format: {"pubkey":"<ResultSubmissionPDA>","ts":"<ISO>"}
 *      The crank reads new lines, deduplicates, and mints promptly (~2s gap).
 *
 *   2. Periodic full scan  (every SCAN_INTERVAL_MS, default: 30 min)
 *      Fetches all ResultSubmission accounts from chain filtered to
 *      status=Confirmed + reward_minted=false. Anything missed by the queue
 *      (e.g. daemon restart, race, old backlog) gets picked up here.
 *
 * Safety:
 *   - reward_minted flag is set on-chain atomically by mint_reward; if the
 *     crank processes the same pubkey twice, the second call gets
 *     "RewardAlreadyMinted" from the program and is silently skipped.
 *   - A local Set (_minted) tracks pubkeys minted this run to avoid sending
 *     duplicate txs within the same process lifetime.
 *   - On 429: exponential back-off up to 32s; after 5 attempts the pubkey is
 *     re-queued for the next scan cycle rather than dropping.
 *   - Queue file is consumed by marking processed lines; a pointer file
 *     (CRANK_QUEUE_FILE + ".pos") tracks the byte offset last read so the
 *     crank never re-reads already-processed lines after restart.
 *
 * Usage:
 *   node life_crank.js                         # daemon mode
 *   node life_crank.js --pilot 15              # process exactly N from backlog then exit
 *   node life_crank.js --dry-run --pilot 15    # as above but no txs
 *
 * Environment / config (all optional, defaults shown):
 *   LIFE_PROGRAM_ID     74RHjg1zYgN9zuVykde4SK2ERiRgNkouATW9MmQDLRWf
 *   SOLANA_RPC          https://api.devnet.solana.com
 *   CRANK_KEYPAIR       /root/.life-compute/wallet.json
 *   CRANK_QUEUE_FILE    output/crank_queue.jsonl   (relative to cwd)
 *   INTER_TX_DELAY_MS   2000
 *   SCAN_INTERVAL_MS    1800000   (30 min)
 *   SCAN_BATCH_DELAY_MS 1200      (delay between RPC calls during full scan)
 */

"use strict";

const anchor = require("@coral-xyz/anchor");
const {
  PublicKey,
  Connection,
  Keypair,
  SystemProgram,
} = require("@solana/web3.js");
const {
  getAssociatedTokenAddress,
  createAssociatedTokenAccountInstruction,
  TOKEN_PROGRAM_ID,
} = require("@solana/spl-token");
const fs   = require("fs");
const path = require("path");

// ── Config ────────────────────────────────────────────────────────────────────
const PROGRAM_ID = new PublicKey(
  process.env.LIFE_PROGRAM_ID || "74RHjg1zYgN9zuVykde4SK2ERiRgNkouATW9MmQDLRWf"
);
const RPC_URL         = process.env.SOLANA_RPC        || "https://api.devnet.solana.com";
const CRANK_KEYPAIR   = process.env.CRANK_KEYPAIR     || "/root/.life-compute/wallet.json";
const QUEUE_FILE      = path.resolve(
  process.env.CRANK_QUEUE_FILE || path.join(__dirname, "output", "crank_queue.jsonl")
);
const POS_FILE        = QUEUE_FILE + ".pos";
const INTER_TX_MS     = parseInt(process.env.INTER_TX_DELAY_MS   || "2000",  10);
const SCAN_INTERVAL   = parseInt(process.env.SCAN_INTERVAL_MS    || "1800000", 10);
const SCAN_BATCH_MS   = parseInt(process.env.SCAN_BATCH_DELAY_MS || "1200",  10);
const IDL_PATH        = path.join(__dirname, "..", "core", "target", "idl", "life_core.json");
// Alternative IDL locations
const IDL_PATHS = [
  IDL_PATH,
  path.join("/tmp", "life-compute", "core", "target", "idl", "life_core.json"),
];

// ResultSubmission layout offsets (938 bytes total)
const OFF_MINER          = 8;
const OFF_TARGET_ID      = 40;   // u16 LE
const OFF_STATUS         = 576;  // enum: 0=Pending,1=Validating,2=Confirmed,3=Rejected
const OFF_REWARD_MINTED  = 742;  // bool
const OFF_CONF_COUNT     = 743;  // u8 — confirming_count used by mint_reward
const OFF_CONF_LIST      = 744;  // [Pubkey; 5] = 160 bytes
const RESULT_SIZE        = 938;

// Status byte values
const STATUS_CONFIRMED = 2;

// ── Logging ───────────────────────────────────────────────────────────────────
function ts() { return new Date().toISOString().replace("T"," ").slice(0,19); }
function log(msg)  { process.stderr.write(`[${ts()}] CRANK  ${msg}\n`); }
function warn(msg) { process.stderr.write(`[${ts()}] CRANK WARN  ${msg}\n`); }
function err(msg)  { process.stderr.write(`[${ts()}] CRANK ERROR ${msg}\n`); }

// ── Helpers ───────────────────────────────────────────────────────────────────
function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

async function withRetry(fn, label, maxAttempts = 5) {
  for (let attempt = 1; attempt <= maxAttempts; attempt++) {
    try {
      return { ok: true, value: await fn() };
    } catch (e) {
      const is429 = e?.message?.includes("429") || e?.status === 429;
      const isTimeout = e?.message?.toLowerCase().includes("timeout");
      if ((is429 || isTimeout) && attempt < maxAttempts) {
        const delay = Math.min(1000 * (2 ** attempt), 32000);
        warn(`[retry ${attempt}/${maxAttempts}] ${label}: ${String(e.message).slice(0,80)} — back-off ${delay}ms`);
        await sleep(delay);
      } else {
        return { ok: false, error: e };
      }
    }
  }
}

function targetIdBytes(id) {
  const b = Buffer.alloc(2);
  b.writeUInt16LE(id);
  return b;
}

function loadIdl() {
  for (const p of IDL_PATHS) {
    if (fs.existsSync(p)) return JSON.parse(fs.readFileSync(p, "utf8"));
  }
  throw new Error(`IDL not found at any of: ${IDL_PATHS.join(", ")}`);
}

// ── Core mint logic ───────────────────────────────────────────────────────────
/**
 * Mint reward for a single ResultSubmission PDA.
 * Returns "minted" | "already_minted" | "not_confirmed" | "error" | "dry_run"
 */
async function mintOne(pubkeyStr, program, connection, crankKp, lifeMintPda, mintAuthPda, networkConfigPda, opts = {}) {
  const { dryRun = false } = opts;

  const resultPda = new PublicKey(pubkeyStr);

  // ── Fetch the account ────────────────────────────────────────────────────
  const infoRes = await withRetry(
    () => connection.getAccountInfo(resultPda, "confirmed"),
    `getAccountInfo(${pubkeyStr.slice(0,12)}…)`
  );
  if (!infoRes.ok) {
    err(`Could not fetch ${pubkeyStr.slice(0,12)}…: ${infoRes.error.message}`);
    return "error";
  }
  const info = infoRes.value;
  if (!info || info.data.length !== RESULT_SIZE) {
    warn(`${pubkeyStr.slice(0,12)}… wrong size (${info?.data?.length}) — skipping`);
    return "error";
  }

  const d = info.data;
  const status      = d[OFF_STATUS];
  const rewardMinted = d[OFF_REWARD_MINTED];

  // Idempotency: on-chain guard — reward_minted already set
  if (rewardMinted !== 0) {
    log(`${pubkeyStr.slice(0,12)}… already minted on-chain — skip`);
    return "already_minted";
  }
  if (status !== STATUS_CONFIRMED) {
    log(`${pubkeyStr.slice(0,12)}… status=${status} (not Confirmed) — skip`);
    return "not_confirmed";
  }

  const minerPubkey       = new PublicKey(d.slice(OFF_MINER, OFF_MINER + 32));
  const targetId          = d.readUInt16LE(OFF_TARGET_ID);
  const confirmingCount   = d[OFF_CONF_COUNT];

  // Derive PDAs
  const [targetPda] = PublicKey.findProgramAddressSync(
    [Buffer.from("target"), targetIdBytes(targetId)],
    PROGRAM_ID
  );
  const [minerAccountPda] = PublicKey.findProgramAddressSync(
    [Buffer.from("miner"), minerPubkey.toBuffer()],
    PROGRAM_ID
  );

  // Miner ATA
  const minerAta = await getAssociatedTokenAddress(lifeMintPda, minerPubkey);

  if (dryRun) {
    log(`[DRY-RUN] would mint for ${pubkeyStr.slice(0,16)}…  target=${targetId}  miner=${minerPubkey.toBase58().slice(0,12)}…  confirmingCount=${confirmingCount}`);
    return "dry_run";
  }

  // Ensure miner ATA exists
  const ataInfoRes = await withRetry(
    () => connection.getAccountInfo(minerAta),
    `getAccountInfo(minerAta)`
  );
  if (ataInfoRes.ok && !ataInfoRes.value) {
    log(`  Creating miner ATA for ${minerPubkey.toBase58().slice(0,12)}…`);
    const createTx = new anchor.web3.Transaction().add(
      createAssociatedTokenAccountInstruction(crankKp.publicKey, minerAta, minerPubkey, lifeMintPda)
    );
    const createRes = await withRetry(
      () => program.provider.sendAndConfirm(createTx, [crankKp]),
      "createMinerATA"
    );
    if (!createRes.ok) {
      err(`Failed to create miner ATA: ${createRes.error.message}`);
      return "error";
    }
  }

  // Build confirming validator ATA list from on-chain confirming_validator_list
  const remainingAccounts = [];
  for (let i = 0; i < Math.min(confirmingCount, 5); i++) {
    const offset = OFF_CONF_LIST + i * 32;
    const valPk = new PublicKey(d.slice(offset, offset + 32));
    if (valPk.equals(PublicKey.default)) continue;
    const valAta = await getAssociatedTokenAddress(lifeMintPda, valPk);
    // Ensure validator ATA exists
    const valAtaInfoRes = await withRetry(
      () => connection.getAccountInfo(valAta),
      `getAccountInfo(valAta ${i})`
    );
    if (valAtaInfoRes.ok && !valAtaInfoRes.value) {
      log(`  Creating validator ATA for ${valPk.toBase58().slice(0,12)}…`);
      const createTx = new anchor.web3.Transaction().add(
        createAssociatedTokenAccountInstruction(crankKp.publicKey, valAta, valPk, lifeMintPda)
      );
      await withRetry(() => program.provider.sendAndConfirm(createTx, [crankKp]), "createValATA");
    }
    remainingAccounts.push({ pubkey: valAta, isSigner: false, isWritable: true });
  }

  // ── Call mint_reward ─────────────────────────────────────────────────────
  const txRes = await withRetry(
    () =>
      program.methods
        .mintReward()
        .accounts({
          crank:           crankKp.publicKey,
          networkConfig:   networkConfigPda,
          lifeMint:        lifeMintPda,
          mintAuthority:   mintAuthPda,
          resultSubmission: resultPda,
          target:          targetPda,
          minerAccount:    minerAccountPda,
          minerAta:        minerAta,
          tokenProgram:    TOKEN_PROGRAM_ID,
          systemProgram:   SystemProgram.programId,
        })
        .remainingAccounts(remainingAccounts)
        .rpc(),
    `mintReward(${pubkeyStr.slice(0,12)}…)`
  );

  if (!txRes.ok) {
    const msg = txRes.error.message || "";
    // On-chain idempotency: program returns RewardAlreadyMinted if cranked twice
    if (msg.includes("RewardAlreadyMinted") || msg.includes("6007")) {
      warn(`${pubkeyStr.slice(0,12)}… RewardAlreadyMinted (concurrent crank?) — safe to ignore`);
      return "already_minted";
    }
    // Permanent errors: miner_account PDA missing/wrong layout — cannot be fixed by retrying.
    // AccountDidNotDeserialize (3003 = 0xbbb) on miner_account means old-epoch or
    // unregistered miner; the program cannot mint for them under current state.
    if (msg.includes("AccountDidNotDeserialize") || msg.includes("3003") || msg.includes("0xbbb")) {
      err(`mintReward PERMANENT-SKIP ${pubkeyStr.slice(0,12)}…: miner_account undeserializable — will not retry`);
      return "permanent_error";
    }
    err(`mintReward failed for ${pubkeyStr.slice(0,12)}…: ${msg.slice(0,140)}`);
    if (txRes.error.logs) {
      const rel = txRes.error.logs.filter(l => l.includes("Error") || l.includes("error"));
      if (rel.length) err(`  logs: ${rel.slice(-2).join(" | ")}`);
    }
    return "error";
  }

  const tx = txRes.value;
  log(`✓ minted  ${pubkeyStr.slice(0,16)}…  target=${targetId}  miner=${minerPubkey.toBase58().slice(0,12)}…  confirmingValidators=${confirmingCount}  tx=${tx.slice(0,20)}…`);
  return "minted";
}

// ── Queue reader (tail new lines since last position) ─────────────────────────
function readNewQueueEntries() {
  if (!fs.existsSync(QUEUE_FILE)) return [];
  const stat = fs.statSync(QUEUE_FILE);
  let pos = 0;
  if (fs.existsSync(POS_FILE)) {
    try { pos = parseInt(fs.readFileSync(POS_FILE, "utf8").trim(), 10) || 0; } catch {}
  }
  if (stat.size <= pos) return [];
  const buf = Buffer.alloc(stat.size - pos);
  const fd = fs.openSync(QUEUE_FILE, "r");
  fs.readSync(fd, buf, 0, buf.length, pos);
  fs.closeSync(fd);
  const text = buf.toString("utf8");
  const lines = text.split("\n").filter(l => l.trim());
  // Advance pos to end of file (only complete lines — stop before partial last line)
  const lastNewline = text.lastIndexOf("\n");
  if (lastNewline >= 0) {
    fs.writeFileSync(POS_FILE, String(pos + lastNewline + 1));
  }
  const entries = [];
  for (const line of lines) {
    try {
      const obj = JSON.parse(line);
      if (obj.pubkey) entries.push(obj.pubkey);
    } catch {}
  }
  return entries;
}

// ── Full chain scan ────────────────────────────────────────────────────────────
async function scanChain(connection) {
  log("Full chain scan — fetching all Confirmed+unminted PDAs…");
  // b58 of bytes [2] = "3" (Confirmed), [0] = "1" (reward_minted=false)
  const res = await withRetry(
    () => connection.getProgramAccounts(PROGRAM_ID, {
      commitment: "confirmed",
      dataSlice:  { offset: 0, length: RESULT_SIZE },
      filters: [
        { dataSize: RESULT_SIZE },
        { memcmp: { offset: OFF_STATUS,        bytes: "3" } },
        { memcmp: { offset: OFF_REWARD_MINTED, bytes: "1" } },
      ],
    }),
    "getProgramAccounts(scan)"
  );
  if (!res.ok) {
    err(`Full scan failed: ${res.error.message}`);
    return [];
  }
  log(`  Scan found ${res.value.length} pending mints`);
  return res.value.map(a => a.pubkey.toBase58());
}

// ── Main ───────────────────────────────────────────────────────────────────────
async function main() {
  const args = process.argv.slice(2);
  const DRY_RUN    = args.includes("--dry-run");
  const pilotIdx   = args.indexOf("--pilot");
  const PILOT_N    = pilotIdx >= 0 ? parseInt(args[pilotIdx + 1], 10) : 0;
  const PILOT_MODE = PILOT_N > 0;

  log(`Starting — DRY_RUN=${DRY_RUN} PILOT=${PILOT_MODE ? PILOT_N : "no"}`);
  log(`  Program:       ${PROGRAM_ID.toBase58()}`);
  log(`  RPC:           ${RPC_URL}`);
  log(`  Queue file:    ${QUEUE_FILE}`);
  log(`  Inter-tx gap:  ${INTER_TX_MS}ms`);
  log(`  Scan interval: ${SCAN_INTERVAL / 60000}min`);

  // ── Init Anchor ────────────────────────────────────────────────────────────
  const crankKp   = Keypair.fromSecretKey(Buffer.from(JSON.parse(fs.readFileSync(CRANK_KEYPAIR, "utf8"))));
  const connection = new Connection(RPC_URL, "confirmed");
  const wallet    = new anchor.Wallet(crankKp);
  const provider  = new anchor.AnchorProvider(connection, wallet, { commitment: "confirmed", skipPreflight: false });
  const idl       = loadIdl();
  idl.address     = PROGRAM_ID.toBase58();
  const program   = new anchor.Program(idl, provider);

  log(`  Crank keypair: ${crankKp.publicKey.toBase58()}`);

  // PDAs
  const [networkConfigPda] = PublicKey.findProgramAddressSync([Buffer.from("network_config")], PROGRAM_ID);
  const [lifeMintPda]      = PublicKey.findProgramAddressSync([Buffer.from("life_mint")],      PROGRAM_ID);
  const [mintAuthPda]      = PublicKey.findProgramAddressSync([Buffer.from("life_mint"), Buffer.from("authority")], PROGRAM_ID);

  // In-process dedup set (reset on each process restart — on-chain flag is the real guard)
  const _minted = new Set();
  // Permanent errors: submissions that cannot be minted under current program state
  // (e.g. miner_account PDA missing or wrong layout). Skipped on re-queue.
  const _permanentError = new Set();
  // Pending queue (pubkeys waiting to be minted)
  const _pending = new Set();

  let mintedTotal = 0;
  let errorTotal  = 0;
  let skipTotal   = 0;

  async function processPending(maxN = Infinity) {
    const toProcess = [..._pending].slice(0, maxN);
    for (const pubkey of toProcess) {
      _pending.delete(pubkey);
      if (_minted.has(pubkey)) {
        log(`${pubkey.slice(0,12)}… in-process dedup — skip`);
        skipTotal++;
        continue;
      }
      const result = await mintOne(pubkey, program, connection, crankKp, lifeMintPda, mintAuthPda, networkConfigPda, { dryRun: DRY_RUN });
      if (result === "minted" || result === "dry_run") {
        _minted.add(pubkey);
        mintedTotal++;
      } else if (result === "already_minted") {
        _minted.add(pubkey);  // track so we don't retry
        skipTotal++;
      } else if (result === "permanent_error") {
        _permanentError.add(pubkey);
        _minted.add(pubkey);  // suppress re-queue
        errorTotal++;
        warn(`${pubkey.slice(0,12)}… added to permanent-skip list (${_permanentError.size} total)`);
      } else if (result === "error") {
        errorTotal++;
        // Transient error — re-queue for next scan cycle.
        _pending.add(pubkey);
      }
      // Inter-tx rate limit
      if (result !== "already_minted" && result !== "dry_run") {
        await sleep(INTER_TX_MS);
      } else {
        await sleep(200);  // short gap for skips
      }
    }
  }

  // ── PILOT MODE ──────────────────────────────────────────────────────────────
  if (PILOT_MODE) {
    log(`Pilot mode: processing up to ${PILOT_N} confirmed-but-unminted submissions`);
    const fromScan = await scanChain(connection);
    for (const p of fromScan.slice(0, PILOT_N)) _pending.add(p);
    log(`  Loaded ${_pending.size} pubkeys for pilot batch`);
    await processPending(PILOT_N);
    log(`\nPilot complete — minted=${mintedTotal} skipped=${skipTotal} errors=${errorTotal}`);
    process.exit(errorTotal > 0 ? 1 : 0);
  }

  // ── DAEMON MODE ─────────────────────────────────────────────────────────────
  // Seed queue from current queue file (catch up on anything accumulated while crank was down)
  const seedEntries = readNewQueueEntries();
  if (seedEntries.length) {
    log(`  Seeded ${seedEntries.length} pubkeys from queue file`);
    for (const p of seedEntries) _pending.add(p);
  }

  // Initial full scan to pick up all existing backlog
  const initialScan = await scanChain(connection);
  for (const p of initialScan) _pending.add(p);
  log(`  Initial scan added ${initialScan.length} pubkeys (${_pending.size} total pending)`);

  let lastScanAt = Date.now();
  let queuePollAt = Date.now();

  log(`Daemon running — ${_pending.size} pending mints at startup`);

  while (true) {
    // Process whatever is in the pending set (one at a time, rate-limited)
    if (_pending.size > 0) {
      await processPending(1);  // process one per loop tick
      continue;
    }

    // Nothing pending: poll for new queue entries every 5s
    const now = Date.now();
    if (now - queuePollAt >= 5000) {
      queuePollAt = now;
      const newEntries = readNewQueueEntries();
      if (newEntries.length) {
        log(`Queue: ${newEntries.length} new pubkey(s) from validator daemon`);
        for (const p of newEntries) {
          if (!_minted.has(p)) _pending.add(p);
        }
      }
    }

    // Periodic full scan safety net
    if (now - lastScanAt >= SCAN_INTERVAL) {
      lastScanAt = now;
      const scanResults = await scanChain(connection);
      let added = 0;
      for (const p of scanResults) {
        if (!_minted.has(p)) { _pending.add(p); added++; }
      }
      log(`Periodic scan: ${scanResults.length} pending on-chain, ${added} newly queued`);
    }

    await sleep(1000);  // idle poll — low CPU
  }
}

main().catch(e => {
  err(`Fatal: ${e.message || e}`);
  if (e.logs) err(`Program logs:\n${e.logs.join("\n")}`);
  process.exit(1);
});
