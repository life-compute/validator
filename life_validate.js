/**
 * life_validate.js — call validate_result on-chain via Anchor.
 * Mirrors life_submit.js from the miner exactly.
 * Args: JSON string { rpc, validatorKeypair, payerKeypair, idlPath, programId,
 *                     resultPubkey, rescoredAffinity }
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

  // Fetch the ResultSubmission account to get miner + epoch + target_id
  const resultAccount = await program.account.resultSubmission.fetch(resultPubkey);
  const epoch         = resultAccount.epoch;
  const targetId      = resultAccount.targetId;
  const minerKey      = resultAccount.miner;

  // Derive PDAs
  const [networkConfig] = web3.PublicKey.findProgramAddressSync(
    [Buffer.from("network_config")], programId
  );
  const [target] = web3.PublicKey.findProgramAddressSync(
    [Buffer.from("target"), Buffer.from([targetId])], programId
  );

  // WeeklyLeaderboard — derive from current week
  const configAccount = await program.account.networkConfig.fetch(networkConfig);
  const slotsPerWeek  = BigInt(1_512_000);
  const currentWeek   = configAccount.currentSlot
    ? BigInt(configAccount.currentSlot) / slotsPerWeek
    : BigInt(0);
  const weekBytes = Buffer.alloc(8);
  weekBytes.writeBigUInt64LE(currentWeek);
  const [leaderboard] = web3.PublicKey.findProgramAddressSync(
    [Buffer.from("leaderboard"), weekBytes, Buffer.from([targetId])], programId
  );

  // ValidationRecord PDA (unique per result × validator)
  const [validationRecord] = web3.PublicKey.findProgramAddressSync(
    [Buffer.from("validation"), resultPubkey.toBuffer(), validatorKP.publicKey.toBuffer()],
    programId
  );

  const tx = await program.methods
    .validateResult(args.rescoredAffinity)
    .accounts({
      payer:            payerKP.publicKey,
      validator:        validatorKP.publicKey,
      networkConfig:    networkConfig,
      target:           target,
      resultSubmission: resultPubkey,
      validationRecord: validationRecord,
      weeklyLeaderboard: leaderboard,
      systemProgram:    web3.SystemProgram.programId,
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
