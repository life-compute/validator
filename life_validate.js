/**
 * life_validate.js — call validate_result on-chain via Anchor.
 * Args: JSON string { rpc, validatorKeypair, payerKeypair, idlPath, programId,
 *                     resultPubkey, rescoredAffinity }
 *
 * Account layout for validate_result (9 accounts, per updated IDL):
 *   0  payer              (mut, signer)
 *   1  validator          (mut, signer)
 *   2  network_config     (readonly)
 *   3  target             (mut) — seeds: ['target', target_id.to_le_bytes() as u16 LE]
 *   4  result_submission  (mut)
 *   5  validation_record  (mut, init) — seeds: ['validation', result_pk, validator_pk]
 *   6  weekly_leaderboard (mut, init_if_needed) — seeds: ['leaderboard', week_u64_le, target_id_u16_le]
 *   7  system_program
 *   8  validator_account  (mut, init_if_needed) — seeds: ['validator_account', validator_pk]
 *
 * Key fixes vs old version:
 *   - target_id is u16 (2 bytes LE), NOT u8 — critical for target + leaderboard PDA derivation
 *   - weekly_leaderboard seeds use current_epoch/7 (u64 LE) + target_id (u16 LE)
 *   - validator_account is the LAST account (pos 8), after system_program (pos 7)
 *   - Let Anchor resolve all PDAs automatically from the correct IDL
 */
const anchor = require("@coral-xyz/anchor");
const web3   = require("@solana/web3.js");
const fs     = require("fs");

async function main() {
  const args = JSON.parse(process.argv[2]);

  const connection   = new web3.Connection(args.rpc, "confirmed");
  const validatorKP  = web3.Keypair.fromSecretKey(
    Uint8Array.from(JSON.parse(fs.readFileSync(args.validatorKeypair, "utf8")))
  );
  const payerKP      = web3.Keypair.fromSecretKey(
    Uint8Array.from(JSON.parse(fs.readFileSync(args.payerKeypair, "utf8")))
  );

  const provider = new anchor.AnchorProvider(
    connection,
    new anchor.Wallet(validatorKP),
    { commitment: "confirmed", preflightCommitment: "confirmed" }
  );
  const idl     = JSON.parse(fs.readFileSync(args.idlPath, "utf8"));
  idl.address   = args.programId;  // always override — prevents stale IDL address after redeployment
  const program  = new anchor.Program(idl, provider);
  const programId = new web3.PublicKey(args.programId);

  const resultPubkey = new web3.PublicKey(args.resultPubkey);

  // Fetch the ResultSubmission account to get target_id (u16) and miner
  const resultAccount = await program.account.resultSubmission.fetch(resultPubkey);
  const targetId      = resultAccount.targetId;   // u16
  const minerKey      = resultAccount.miner;

  // Static PDAs
  const [networkConfig] = web3.PublicKey.findProgramAddressSync(
    [Buffer.from("network_config")], programId
  );

  // target_id is u16 LE (2 bytes) — this was the critical bug in the old version
  const targetIdBuf = Buffer.alloc(2);
  targetIdBuf.writeUInt16LE(targetId);
  const [target] = web3.PublicKey.findProgramAddressSync(
    [Buffer.from("target"), targetIdBuf], programId
  );

  // ValidationRecord: seeds = ['validation', result_pubkey, validator_pubkey]
  const [validationRecord] = web3.PublicKey.findProgramAddressSync(
    [Buffer.from("validation"), resultPubkey.toBuffer(), validatorKP.publicKey.toBuffer()],
    programId
  );

  // WeeklyLeaderboard: seeds = ['leaderboard', current_week_u64_le, target_id_u16_le]
  // current_week = networkConfig.currentEpoch / 7
  const configAccount = await program.account.networkConfig.fetch(networkConfig);
  const currentEpoch  = BigInt(configAccount.currentEpoch.toString());
  const currentWeek   = currentEpoch / BigInt(7);
  const weekBuf       = Buffer.alloc(8);
  weekBuf.writeBigUInt64LE(currentWeek);
  const [leaderboard] = web3.PublicKey.findProgramAddressSync(
    [Buffer.from("leaderboard"), weekBuf, targetIdBuf], programId
  );

  // ValidatorAccount: seeds = ['validator_account', validator_pubkey]
  const [validatorAccount] = web3.PublicKey.findProgramAddressSync(
    [Buffer.from("validator_account"), validatorKP.publicKey.toBuffer()], programId
  );

  // Submit validate_result on-chain
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
      systemProgram:     web3.SystemProgram.programId,
      validatorAccount:  validatorAccount,
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
