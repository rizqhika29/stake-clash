# StakeClash

P2P head-to-head sports betting contract on GenLayer. Two users challenge each other on real-world sports outcomes - one creates the bet, the other accepts and funds it - and the contract settles automatically using AI-powered validator consensus.

## Deployed Contract

**Address:** `0x10E89F52265Fa853aCB499E81709184cb6763B25`
**Explorer:** https://explorer-studio.genlayer.com/address/0x10E89F52265Fa853aCB499E81709184cb6763B25

## How It Works

1. Creator calls `create_bet()` with opponent, description, resolution URL, criteria, and stake
2. Creator funds their stake via `fund_creator_stake()` or inline in `create_bet()`
3. Opponent accepts and funds via `accept_and_fund_bet()`
4. After both fund, anyone calls `resolve_bet()` - AI validators fetch the URL and determine the winner
5. Winner receives both stakes; draw refunds both

## Security Features

- **Caller restriction:** Only creator/opponent can resolve before timeout; anyone after timeout
- **Anti double-fund:** Each party can only fund once
- **Per-bet fund isolation:** Each bet tracks its own balances
- **Atomic state+transfer:** State changes and transfers happen together
- **Timeout escape:** Creator can cancel if opponent never funds

## Tests

**Direct tests (56):**
```bash
python -m pytest tests/direct/ -v
```

**Live tests (35) on GenLayer Studio:**
```bash
python tools/live_test.py 0x10E89F52265Fa853aCB499E81709184cb6763B25
```
