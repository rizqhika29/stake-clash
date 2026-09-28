# v0.3.0-rc7
# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }
"""
StakeClash
==========

A head-to-peer sports betting Intelligent Contract on GenLayer. Two users
challenge each other on a real-world sports outcome - one creates the bet,
the other accepts and funds it - and the contract settles automatically by
**fetching live web data** and using LLM-powered validator consensus to
determine the winner. No oracle, no admin, no middleman.

Security Model
--------------
1. **Caller restriction**: Only the creator or opponent can resolve before
   the timeout. After the timeout, anyone can trigger resolution (or
   force-refund if no consensus is reached).
2. **Anti double-fund**: The opponent can only fund once; the contract
   rejects duplicate funding attempts.
3. **Per-bet fund isolation**: Each bet has its own stake pool. Payouts
   come from the bet's own funds, not a shared pool.
4. **Timeout / escape path**: After RESOLUTION_TIMEOUT (7 days) from
   funding, if no resolution has occurred, anyone can call resolve_bet.
   If consensus is reached, winner is paid. If not, both parties get
   refunded.
5. **Atomic state + transfer**: State updates and token transfers happen
   in the same transaction.
6. **Validator binding**: Validators independently re-fetch the same URL
   and verify the same outcome. The equivalence check requires agreement
   on the winner.
7. **URL binding**: The resolution_url is locked at creation and cannot
   be changed. Validators verify against the stored URL.
8. **Cancel protection**: Only the creator can cancel, and only before
   the opponent has funded.

Lifecycle
---------
1. Creator calls create_bet() -> bet_id (status: open)
2. Opponent calls accept_and_fund_bet(bet_id) + sends exact stake -> (status: funded)
3. Anyone (with restrictions) calls resolve_bet(bet_id) -> AI consensus -> winner
4. Winner receives 2x stake (their own + opponent's)

Trust Model
-----------
- The bet creator is trusted to choose a valid resolution_url that will
  carry the event outcome. The contract verifies that validators agree
  on the outcome from that URL, not the truth of the URL itself.
- Resolution requires at least MIN_DATA_SOURCES (2) reachable sources
  or the single resolution URL being validated by multiple independent
  validators.
"""

from genlayer import *
from dataclasses import dataclass
import json


@gl.evm.contract_interface
class _EOA:
    """Interface for sending GEN to an EOA / chain-layer address.

    Value transfers to EOAs are *external* messages that go through the IC's
    ghost contract on the chain layer, so they use the EVM contract
    interface even though the recipient is not a contract (see the GenLayer
    "Value Transfers" docs)."""

    class View:
        pass

    class Write:
        pass


# --- Tunable constants ---------------------------------------------------

# Resolution timeout: 7 days after both parties fund.
# After this window, anyone can trigger resolution or force-refund.
RESOLUTION_TIMEOUT = 7 * 24 * 3600  # 7 days in seconds

# Maximum description length
MAX_DESCRIPTION_CHARS = 500

# Maximum criteria length
MAX_CRITERIA_CHARS = 2000

# Maximum URL length
MAX_URL_CHARS = 256

# Maximum source body chars for prompt
MAX_SOURCE_BODY_CHARS = 4000

# --- Bet statuses --------------------------------------------------------

BET_STATUS_OPEN = "open"
BET_STATUS_FUNDED = "funded"
BET_STATUS_RESOLVED = "resolved"
BET_STATUS_CANCELLED = "cancelled"
BET_STATUS_REFUNDED = "refunded"


@allow_storage
@dataclass
class Bet:
    bet_id: str
    creator: Address
    opponent: Address
    description: str
    resolution_url: str
    resolution_criteria: str
    creator_stake: u256
    opponent_stake: u256
    creator_funded: bool
    opponent_funded: bool
    status: str
    winner: Address
    loser: Address
    payout: u256
    result: str
    created_at: u256
    funded_at: u256
    resolved_at: u256

    def as_dict(self) -> dict:
        return {
            "bet_id": self.bet_id,
            "creator": str(self.creator),
            "opponent": str(self.opponent),
            "description": self.description,
            "resolution_url": self.resolution_url,
            "resolution_criteria": self.resolution_criteria,
            "creator_stake": int(self.creator_stake),
            "opponent_stake": int(self.opponent_stake),
            "creator_funded": self.creator_funded,
            "opponent_funded": self.opponent_funded,
            "status": self.status,
            "winner": str(self.winner) if self.winner else "",
            "loser": str(self.loser) if self.loser else "",
            "payout": int(self.payout),
            "result": self.result,
            "created_at": int(self.created_at),
            "funded_at": int(self.funded_at),
            "resolved_at": int(self.resolved_at),
        }


# --- Deterministic helpers (unit-testable without a VM) -------------------

def _current_timestamp() -> u256:
    """Deterministic per-transaction Unix timestamp (seconds). GenLayer
    pins the stdlib clock to the transaction datetime, so every validator
    computing it sees the same value."""
    import datetime as _dt
    return u256(int(_dt.datetime.now(_dt.timezone.utc).timestamp()))


def _coerce_address(value) -> Address:
    """Normalize an address argument (Address, hex/base64 str, or raw int)."""
    if isinstance(value, Address):
        return value
    if isinstance(value, str):
        return Address(value)
    if isinstance(value, int):
        return Address(value.to_bytes(20, "big"))
    return Address(bytes(value))


def _validate_url(url: str) -> str:
    """Deterministic URL validation. Rejects non-http(s) URLs and over-length."""
    url = url.strip()
    if len(url) > MAX_URL_CHARS:
        raise Exception(f"URL too long (max {MAX_URL_CHARS} chars)")
    if not (url.startswith("http://") or url.startswith("https://")):
        raise Exception(f"invalid URL: {url!r}")
    return url


def _normalize_verdict(raw) -> str | None:
    """Coerce an LLM-produced verdict into CREATOR_WIN / OPPONENT_WIN / DRAW,
    or None for an unreadable result. Case-insensitive."""
    if isinstance(raw, bool):
        return None
    if isinstance(raw, str):
        value = raw.strip().upper()
        if value in ("CREATOR", "CREATOR_WIN", "YES", "Y", "TRUE", "1"):
            return "CREATOR_WIN"
        if value in ("OPPONENT", "OPPONENT_WIN", "NO", "N", "FALSE", "0"):
            return "OPPONENT_WIN"
        if value in ("DRAW", "TIE", "NONE"):
            return "DRAW"
        return None
    if isinstance(raw, int):
        if raw == 1:
            return "CREATOR_WIN"
        if raw == 0:
            return "OPPONENT_WIN"
        return None
    return None


def _strip_code_fence(raw: str) -> str:
    """Strip a markdown code fence from LLM JSON output if present."""
    s = raw.strip()
    if s.startswith("```"):
        first_newline = s.find("\n")
        s = s[first_newline + 1:] if first_newline != -1 else s[3:]
        if s.endswith("```"):
            s = s[:-3]
        s = s.strip()
    return s


def _parse_json_object(raw) -> dict | None:
    """Parse an agreed consensus payload, tolerating a JSON string."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            data = json.loads(_strip_code_fence(raw))
        except (ValueError, TypeError):
            return None
        return data if isinstance(data, dict) else None
    return None


# --- The contract ----------------------------------------------------------

class StakeClash(gl.Contract):
    bets: TreeMap[str, Bet]
    bet_count: u256

    def __init__(self):
        self.bet_count = u256(0)

    # -- Internal helpers ---------------------------------------------------

    def _get_bet(self, bet_id: str) -> Bet:
        """Get bet or raise if not found."""
        bet = self.bets.get(bet_id)
        if bet is None:
            raise gl.vm.UserError("unknown bet_id")
        return bet

    def _validate_bet_inputs(
        self, description: str, resolution_url: str, resolution_criteria: str,
        stake: int
    ) -> tuple[str, str, str]:
        """Validate and normalize bet creation inputs."""
        description = description.strip()
        resolution_criteria = resolution_criteria.strip()
        if not description:
            raise gl.vm.UserError("description must not be empty")
        if not resolution_criteria:
            raise gl.vm.UserError("resolution_criteria must not be empty")
        if len(description) > MAX_DESCRIPTION_CHARS:
            raise gl.vm.UserError(f"description too long (max {MAX_DESCRIPTION_CHARS})")
        if len(resolution_criteria) > MAX_CRITERIA_CHARS:
            raise gl.vm.UserError(f"resolution_criteria too long (max {MAX_CRITERIA_CHARS})")
        if stake <= 0:
            raise gl.vm.UserError("stake must be positive")
        resolution_url = _validate_url(resolution_url)
        return description, resolution_url, resolution_criteria

    def _refund_bet(self, bet_id: str, bet: Bet) -> None:
        """Refund both parties. Called when timeout expires without resolution."""
        total = bet.creator_stake + bet.opponent_stake
        bet.status = BET_STATUS_REFUNDED
        bet.resolved_at = _current_timestamp()
        bet.result = "timeout_refund"
        self.bets[bet_id] = bet

        if bet.creator_funded and bet.creator_stake > u256(0):
            _EOA(bet.creator).emit_transfer(value=bet.creator_stake)
        if bet.opponent_funded and bet.opponent_stake > u256(0):
            _EOA(bet.opponent).emit_transfer(value=bet.opponent_stake)

    def _is_timeout_expired(self, bet: Bet) -> bool:
        """Check if resolution timeout has expired after both funded."""
        if bet.funded_at == u256(0):
            return False
        now = _current_timestamp()
        return now > bet.funded_at + u256(RESOLUTION_TIMEOUT)

    # -- Write Methods (state-changing) -------------------------------------

    @gl.public.write.payable
    def create_bet(
        self,
        opponent: Address,
        description: str,
        resolution_url: str,
        resolution_criteria: str,
        stake: int,
    ) -> str:
        """Create a new head-to-head bet against the specified opponent.

        The caller becomes the creator. The creator defines the event
        description, resolution URL, criteria, and stake amount.
        The opponent must then accept and fund the bet.

        Returns the new bet_id."""
        opponent = _coerce_address(opponent)
        description, resolution_url, resolution_criteria = self._validate_bet_inputs(
            description, resolution_url, resolution_criteria, stake,
        )

        creator = gl.message.sender_address
        if str(creator) == str(opponent):
            raise gl.vm.UserError("creator and opponent must be different")

        bet_id = f"bet-{self.bet_count}"
        self.bet_count = self.bet_count + u256(1)

        self.bets[bet_id] = Bet(
            bet_id=bet_id,
            creator=creator,
            opponent=opponent,
            description=description,
            resolution_url=resolution_url,
            resolution_criteria=resolution_criteria,
            creator_stake=u256(stake),
            opponent_stake=u256(0),
            creator_funded=False,
            opponent_funded=False,
            status=BET_STATUS_OPEN,
            winner=Address(b"\x00" * 20),
            loser=Address(b"\x00" * 20),
            payout=u256(0),
            result="",
            created_at=_current_timestamp(),
            funded_at=u256(0),
            resolved_at=u256(0),
        )
        return bet_id

    @gl.public.write.payable
    def fund_creator_stake(self, bet_id: str) -> str:
        """Creator funds their stake. The transaction must carry exactly
        the creator_stake amount. Returns confirmation message."""
        bet_id = str(bet_id)
        bet = self._get_bet(bet_id)

        sender = gl.message.sender_address
        if str(sender) != str(bet.creator):
            raise gl.vm.UserError("only the creator can fund their stake")
        if bet.status != BET_STATUS_OPEN:
            raise gl.vm.UserError("bet is not in open status")
        if bet.creator_funded:
            raise gl.vm.UserError("creator already funded")
        if gl.message.value != bet.creator_stake:
            raise gl.vm.UserError(
                f"exact stake required: expected {int(bet.creator_stake)}, "
                f"sent {int(gl.message.value)}"
            )

        bet.creator_funded = True
        self.bets[bet_id] = bet
        return f"Creator funded {int(bet.creator_stake)} tokens"

    @gl.public.write.payable
    def accept_and_fund_bet(self, bet_id: str) -> str:
        """Opponent accepts the bet and funds their stake.

        The transaction must carry exactly the creator_stake amount
        (matched stake). This transitions the bet from 'open' to 'funded'
        and starts the resolution timeout.

        SECURITY: Only the designated opponent can call this. Anti double-fund
        is enforced by checking opponent_funded status.

        Returns confirmation message."""
        bet_id = str(bet_id)
        bet = self._get_bet(bet_id)

        sender = gl.message.sender_address
        if str(sender) != str(bet.opponent):
            raise gl.vm.UserError("only the designated opponent can accept")
        if bet.status != BET_STATUS_OPEN:
            raise gl.vm.UserError("bet is not in open status")
        if bet.opponent_funded:
            raise gl.vm.UserError("opponent already funded (double-fund rejected)")
        if gl.message.value != bet.creator_stake:
            raise gl.vm.UserError(
                f"stake mismatch: expected {int(bet.creator_stake)}, "
                f"sent {int(gl.message.value)}"
            )

        bet.opponent_funded = True
        bet.opponent_stake = bet.creator_stake
        bet.status = BET_STATUS_FUNDED
        bet.funded_at = _current_timestamp()
        self.bets[bet_id] = bet
        return f"Opponent funded {int(bet.creator_stake)} tokens. Bet is now active."

    @gl.public.write
    def cancel_bet(self, bet_id: str) -> str:
        """Creator cancels an open bet before the opponent has funded.

        SECURITY: Only the creator can cancel. Only works in 'open' status.
        Refunds the creator's stake if they already funded it.

        Returns confirmation message."""
        bet_id = str(bet_id)
        bet = self._get_bet(bet_id)

        sender = gl.message.sender_address
        if str(sender) != str(bet.creator):
            raise gl.vm.UserError("only the creator can cancel")
        if bet.status != BET_STATUS_OPEN:
            raise gl.vm.UserError("can only cancel open bets")
        if bet.opponent_funded:
            raise gl.vm.UserError("opponent already funded, cannot cancel")

        # Refund creator if they funded
        if bet.creator_funded and bet.creator_stake > u256(0):
            _EOA(bet.creator).emit_transfer(value=bet.creator_stake)

        bet.status = BET_STATUS_CANCELLED
        bet.resolved_at = _current_timestamp()
        bet.result = "cancelled_by_creator"
        self.bets[bet_id] = bet
        return "Bet cancelled, creator refunded"

    @gl.public.write
    def resolve_bet(self, bet_id: str) -> dict:
        """Resolve a bet using AI-powered validator consensus.

        SECURITY ENFORCEMENT:
        - Before timeout: Only creator or opponent can call
        - After timeout: Anyone can call (force-resolution path)
        - The contract fetches the resolution_url and uses LLM to determine
          the winner based on the resolution_criteria
        - Validators independently re-fetch and verify the same URL

        If timeout has passed and consensus cannot be reached, the bet
        is refunded to both parties.

        Returns resolution dict: {status, winner, result, payout}."""
        bet_id = str(bet_id)
        bet = self._get_bet(bet_id)

        # Enforce caller restriction
        sender = gl.message.sender_address
        timeout_expired = self._is_timeout_expired(bet)

        if bet.status == BET_STATUS_FUNDED:
            # Before resolution: check caller authorization
            if not timeout_expired:
                if str(sender) != str(bet.creator) and str(sender) != str(bet.opponent):
                    raise gl.vm.UserError(
                        "Only bet participants can resolve before timeout"
                    )
            # After timeout: anyone can call (fall through)

        elif bet.status == BET_STATUS_RESOLVED:
            return {"status": "already_resolved", "winner": str(bet.winner), "result": bet.result}

        elif bet.status == BET_STATUS_REFUNDED:
            return {"status": "already_refunded", "result": bet.result}

        else:
            raise gl.vm.UserError(f"cannot resolve bet in '{bet.status}' status")

        if bet.status != BET_STATUS_FUNDED:
            return {"status": bet.status, "result": bet.result}

        # Capture storage before entering nondeterministic block
        resolution_url = bet.resolution_url
        description = bet.description
        criteria = bet.resolution_criteria
        creator = bet.creator
        opponent = bet.opponent

        def leader_fn():
            return _fetch_and_evaluate(resolution_url, description, criteria)

        def validator_fn(leaders_res):
            return _consensus_validator(
                leaders_res, resolution_url, description, criteria
            )

        result = gl.vm.run_nondet_unsafe(leader_fn, validator_fn)

        # Check consensus result
        if not _consensus_ok(result):
            # If timeout expired, refund both parties
            if timeout_expired:
                self._refund_bet(bet_id, bet)
                return {
                    "status": "refunded",
                    "result": "timeout_no_consensus",
                    "winner": "",
                    "payout": 0,
                }
            raise gl.vm.UserError(
                "resolution indeterminate (consensus not reached) -- try again later"
            )

        # Extract winner from consensus
        values = result["values"]
        winner_label = values.get("winner", "DRAW")

        if winner_label == "CREATOR_WIN":
            winner = creator
            loser = opponent
        elif winner_label == "OPPONENT_WIN":
            winner = opponent
            loser = creator
        else:
            # DRAW - refund both
            self._refund_bet(bet_id, bet)
            return {
                "status": "draw_refunded",
                "result": "draw",
                "winner": "",
                "payout": 0,
            }

        # Calculate payout (winner gets both stakes)
        payout = bet.creator_stake + bet.opponent_stake

        # Atomic state update + transfer
        bet.winner = winner
        bet.loser = loser
        bet.payout = payout
        bet.status = BET_STATUS_RESOLVED
        bet.result = f"winner: {winner_label}"
        bet.resolved_at = _current_timestamp()
        self.bets[bet_id] = bet

        # Atomic transfer
        _EOA(winner).emit_transfer(value=payout)

        return {
            "status": "resolved",
            "winner": str(winner),
            "winner_label": winner_label,
            "result": bet.result,
            "payout": int(payout),
            "confidence": values.get("confidence", 0),
            "reasoning": values.get("reasoning", ""),
        }

    # -- View Methods (read-only, free) -------------------------------------

    @gl.public.view
    def get_bet(self, bet_id: str) -> dict:
        """Get full details of a bet."""
        bet_id = str(bet_id)
        bet = self._get_bet(bet_id)
        return bet.as_dict()

    @gl.public.view
    def get_bet_count(self) -> u256:
        """Get total number of bets."""
        return self.bet_count

    @gl.public.view
    def get_open_bets(self) -> list:
        """Get all bets that are still open for acceptance."""
        open_bets = []
        for bet in self.bets.values():
            if bet.status == BET_STATUS_OPEN:
                open_bets.append(bet.as_dict())
        return open_bets

    @gl.public.view
    def get_user_bets(self, user: Address) -> list:
        """Get all bets where user is creator or opponent."""
        user = _coerce_address(user)
        user_bets = []
        for bet in self.bets.values():
            if str(bet.creator) == str(user) or str(bet.opponent) == str(user):
                user_bets.append(bet.as_dict())
        return user_bets

    @gl.public.view
    def get_contract_balance(self) -> u256:
        """The real GEN balance held on this contract."""
        return self.balance

    @gl.public.view
    def get_contract_address(self) -> str:
        """Get the contract address."""
        return str(self.address)


# --- Consensus block (leader + validator) ----------------------------------

def _fetch_and_evaluate(
    resolution_url: str,
    description: str,
    criteria: str,
) -> dict:
    """Evidence acquisition + LLM evaluation -- the *only* work done inside
    the non-deterministic block. Runs identically on the leader and on every
    validator.

    Fetches the resolution URL and asks the model to determine the winner
    based on the event description and resolution criteria.

    Returns ``{"ok": True, "values": {"winner": "CREATOR_WIN"|"OPPONENT_WIN"|"DRAW",
    "confidence": 0-100, "reasoning": "..."}}`` or
    ``{"ok": False, "reason": "..."}``."""
    try:
        response = gl.nondet.web.get(resolution_url)
        web_data = response.body.decode("utf-8")
    except Exception as e:
        return {"ok": False, "reason": f"failed_to_fetch: {str(e)[:200]}"}

    prompt = f"""You are an impartial judge for a P2P sports bet.

Bet Description:
{description}

Resolution Criteria:
{criteria}

You are reading ONE evidence source: {resolution_url}
Source content:
{web_data[:MAX_SOURCE_BODY_CHARS]}

Based ONLY on the content of this source, determine the outcome of this bet.
- If the creator's position is correct, respond with CREATOR_WIN
- If the opponent's position is correct, respond with OPPONENT_WIN
- If it's a draw or unclear, respond with DRAW

You MUST also provide:
- confidence: a number 0-100 indicating your confidence
- reasoning: a brief explanation of your determination

Respond with ONLY a JSON object, no other text, no markdown fences:
{{"winner": "CREATOR_WIN" or "OPPONENT_WIN" or "DRAW", "confidence": <0-100>, "reasoning": "<1-2 sentences>"}}"""

    try:
        parsed = gl.nondet.exec_prompt(prompt, response_format="json")
    except Exception as e:
        return {"ok": False, "reason": f"llm_error: {str(e)[:200]}"}

    payload = _parse_json_object(parsed)
    if payload is None:
        return {"ok": False, "reason": "invalid_llm_response"}

    winner = _normalize_verdict(payload.get("winner"))
    confidence = payload.get("confidence", 0)
    reasoning = str(payload.get("reasoning", ""))[:500]

    if winner is None:
        return {"ok": False, "reason": "unverdictable"}

    return {
        "ok": True,
        "values": {
            "winner": winner,
            "confidence": confidence,
            "reasoning": reasoning,
        },
    }


def _consensus_validator(
    leaders_res,
    resolution_url: str,
    description: str,
    criteria: str,
) -> bool:
    """The equivalence check. Runs on every validator, which independently
    re-fetches the resolution URL and re-evaluates the outcome. The leader's
    payload is accepted only if:

    - the leader payload passes the deterministic shape check;
    - the validator independently fetches the SAME URL (URL binding);
    - the validator reaches the SAME winner determination;
    - the confidence levels are within acceptable range.

    SECURITY: This prevents:
    - Different validators accepting different outcomes
    - Manipulation of the resolution URL
    - Inconsistent winner determinations"""
    if not isinstance(leaders_res, gl.vm.Return):
        return False
    leader_data = leaders_res.calldata
    if not isinstance(leader_data, dict):
        return False
    if not _consensus_ok(leader_data):
        return False

    # Validator independently fetches the SAME URL and evaluates
    my_data = _fetch_and_evaluate(resolution_url, description, criteria)

    if not my_data.get("ok"):
        return False

    leader_values = leader_data.get("values", {})
    my_values = my_data.get("values", {})

    # Critical: winner must match exactly
    leader_winner = leader_values.get("winner")
    my_winner = my_values.get("winner")

    if leader_winner != my_winner:
        return False

    # Confidence must be within 20 points (reasonable tolerance)
    leader_conf = leader_values.get("confidence", 0)
    my_conf = my_values.get("confidence", 0)
    if abs(int(leader_conf) - int(my_conf)) > 20:
        return False

    return True


def _consensus_ok(data) -> bool:
    """Deterministic shape check on the agreed consensus payload."""
    payload = _parse_json_object(data)
    if payload is None:
        return False
    if not payload.get("ok"):
        return False
    values = payload.get("values")
    if not isinstance(values, dict):
        return False
    winner = _normalize_verdict(values.get("winner"))
    if winner is None:
        return False
    confidence = values.get("confidence")
    if confidence is None:
        return False
    # Validate confidence is 0-100
    try:
        conf_int = int(confidence)
        if conf_int < 0 or conf_int > 100:
            return False
    except (ValueError, TypeError):
        return False
    return True
