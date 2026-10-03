# StakeClash

P2P head-to-head sports betting contract on GenLayer. Two users challenge each other on real-world sports outcomes - one creates the bet, the other accepts and funds it - and the contract settles automatically using AI-powered validator consensus.

## Deployed Contract

**Address:** `0x7c557cc043d7F8e338A6CCF70f8c9fB9CA5De916`
**Explorer:** https://explorer-studio.genlayer.com/address/0x7c557cc043d7F8e338A6CCF70f8c9fB9CA5De916

## How It Works

1. Creator calls `create_bet()` with opponent, description, resolution URL, criteria, and stake (sending value = stake escrows immediately; 0 funds later via `fund_creator_stake()`)
2. Opponent calls `accept_and_fund_bet()` with the exact matched stake - only allowed once the creator's stake is escrowed
3. After both fund, `resolve_bet()` fetches the URL and uses AI validator consensus to determine the winner
4. Winner receives both stakes; a genuine final draw refunds both

## Security Features (GenLayer steward review)

1. **Exact escrow before FUNDED:** a bet only enters `funded` when both exact stakes are actually escrowed. `create_bet` accounts for any value it receives (0 => fund later, exact stake => escrowed now, anything else => rejected); `accept_and_fund_bet` refuses to transition until the creator's stake is escrowed.
2. **Event-finality gate:** resolution only reaches a terminal outcome when the event is FINAL. An unfinished or unclear event returns a retryable `not_final` status and can never trigger an immediate terminal draw refund.
3. **Deterministic timeout refund:** `timeout_refund()` is a fully deterministic path (no LLM / web / nondeterministic consensus) that refunds both parties once the resolution timeout expires.

Additional hardening: caller restriction, anti double-fund, per-bet fund isolation, atomic state+transfer, validator binding on winner and finality, URL binding, cancel protection.

## Tests

**Direct tests (71):**
```bash
python -m pytest tests/direct/ -v
```

**Integration tests (17) against the deployment:**
```bash
STAKE_CLASH_ADDRESS=0x7c557cc043d7F8e338A6CCF70f8c9fB9CA5De916 python -m pytest tests/integration/
```

**Live tests (40) on GenLayer Studio:**
```bash
python tools/live_test.py 0x7c557cc043d7F8e338A6CCF70f8c9fB9CA5De916
```
