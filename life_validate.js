/**
 * life_validate.js — call validate_result on-chain via Anchor.
 * Args: JSON string { rpcSource, heliusEnvPath, validatorKeypair, payerKeypair,
 *                     idlPath, programId, resultPubkey, rescoredAffinity }
 *
 * The RPC URL is NOT passed in argv: a Helius endpoint carries ?api-key=… and
 * argv is world-readable via /proc/<pid>/cmdline.  This script resolves the
 * endpoint itself with the same priority as validator_daemon.py and
 * life_crank.js: SOLANA_RPC → HELIUS_API_KEY in ~/.helius.env → public devnet.
 *
 * Account layout for validate_result (9 accounts, deployed program):
 *   0  payer              (mut, signer)
 *   1  validator          (signer)
 *   2  network_config     (readonly, PDA)
 *   3  target             (mut, PDA)
 *   4  result_submission  (mut, PDA)
 *   5  validation_record  (mut, init, PDA)
 *   6  weekly_leaderboard (mut, init_if_needed, PDA)
 *   7  validator_account  (mut, init_if_needed, PDA)
 *   8  system_program
 *
 * Returns { tx, confirmed: true }.
 */
const anchor   = require("@coral-xyz/anchor");
const web3     = require("@solana/web3.js");
const fs       = require("fs");

// ── RPC resolution (key never enters argv, env, or any log line) ─────────────
function readHeliusKey(p) {
  try {
    for (const raw of fs.readFileSync(p, "utf8").split("\n")) {
      let line = raw.trim();
      if (!line || line.startsWith("#")) continue;
      if (line.startsWith("export ")) line = line.slice(7).trim();
      const i = line.indexOf("=");
      if (i < 0) continue;
      if (line.slice(0, i).trim() !== "HELIUS_API_KEY") continue;
      return line.slice(i + 1).trim().replace(/^['"]|['"]$/g, "");
    }
  } catch (_) { /* missing/unreadable → caller falls back */ }
  return "";
}

function resolveRpc(heliusEnvPath) {
  if (process.env.SOLANA_RPC) return process.env.SOLANA_RPC;
  const key = readHeliusKey(heliusEnvPath || `${process.env.HOME}/.helius.env`);
  if (key) return `https://devnet.helius-rpc.com/?api-key=${key}`;
  return "https://api.devnet.solana.com";
}

async function main() {
  const args = JSON.parse(process.argv[2]);

  const connection  = new web3.Connection(resolveRpc(args.heliusEnvPath), "confirmed");
  const validatorKP = web3.Keypair.fromSecretKey(
    Uint8Array.from(JSON.parse(fs.readFileSync(args.validatorKeypair, "utf8")))
  );
  const payerKP = web3.Keypair.fromSecretKey(
    Uint8Array.from(JSON.parse(fs.readFileSync(args.payerKeypair, "utf8")))
  );

  const provider = new anchor.AnchorProvider(
    connection,
    new anchor.Wallet(validatorKP),
    { commitment: "confirmed", preflightCommitment: "confirmed" }
  );
  const idl    = JSON.parse(fs.readFileSync(args.idlPath, "utf8"));
  idl.address  = args.programId;
  delete idl.events;  // strip events — avoids Anchor SDK decode errors
  const program = new anchor.Program(idl, provider);
  const programId = new web3.PublicKey(args.programId);

  const resultPubkey = new web3.PublicKey(args.resultPubkey);

  const resultAccount = await program.account.resultSubmission.fetch(resultPubkey);
  const targetId      = resultAccount.targetId;

  const [networkConfig] = web3.PublicKey.findProgramAddressSync(
    [Buffer.from("network_config")], programId
  );

  const targetIdBuf = Buffer.alloc(2);
  targetIdBuf.writeUInt16LE(targetId);
  const [target] = web3.PublicKey.findProgramAddressSync(
    [Buffer.from("target"), targetIdBuf], programId
  );

  const [validationRecord] = web3.PublicKey.findProgramAddressSync(
    [Buffer.from("validation"), resultPubkey.toBuffer(), validatorKP.publicKey.toBuffer()],
    programId
  );

  const configAccount = await program.account.networkConfig.fetch(networkConfig);
  const currentEpoch  = BigInt(configAccount.currentEpoch.toString());
  const currentWeek   = currentEpoch / BigInt(7);
  const weekBuf       = Buffer.alloc(8);
  weekBuf.writeBigUInt64LE(currentWeek);
  const [leaderboard] = web3.PublicKey.findProgramAddressSync(
    [Buffer.from("leaderboard"), weekBuf, targetIdBuf], programId
  );

  const [validatorAccount] = web3.PublicKey.findProgramAddressSync(
    [Buffer.from("validator_account"), validatorKP.publicKey.toBuffer()], programId
  );

  const tx = await program.methods
    .validateResult(args.rescoredAffinity)
    .accounts({
      payer:             payerKP.publicKey,
      validator:         validatorKP.publicKey,
      networkConfig:     networkConfig,
      target:            target,
      resultSubmission:  resultPubkey,
      validationRecord:  validationRecord,
      weeklyLeaderboard: leaderboard,
      validatorAccount:  validatorAccount,
      systemProgram:     web3.SystemProgram.programId,
    })
    .signers([payerKP, validatorKP])
    .rpc();

  process.stdout.write(JSON.stringify({ tx, confirmed: true }) + "\n");
  process.exit(0);
}

main().catch(e => {
  process.stdout.write(JSON.stringify({ error: e.message }) + "\n");
  process.exit(1);
});
