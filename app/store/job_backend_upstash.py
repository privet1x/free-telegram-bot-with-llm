"""Atomic Upstash Redis backend for durable Telegram jobs.

Every state transition that reads and then writes Redis data is implemented in
Lua.  This keeps leases, fencing tokens, terminal transitions, and delivery
checkpoints correct when several serverless invocations race each other.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Mapping, Sequence

from app.store.redis import UPSTASH_LOCK_TIMEOUT_SECONDS, build_upstash_redis


_BASE_LUA = r"""
local function integer(raw, default)
    if raw == false or raw == nil then return default end
    local value = tonumber(raw)
    if value == nil or value % 1 ~= 0 then return default end
    return value
end

local function key_expires_at(key)
    return integer(redis.call('EXPIRETIME', key), -2)
end

local function load_job(now)
    if redis.call('EXISTS', KEYS[1]) == 0 then return nil end
    local expires_at = integer(redis.call('HGET', KEYS[1], 'expires_at'), 0)
    if expires_at <= now then
        redis.call('DEL', KEYS[1], KEYS[2], KEYS[3])
        return nil
    end
    return expires_at
end
"""

_STATE_LUA = (
    _BASE_LUA
    + r"""
local terminal_states = {
    delivered = true,
    failed = true,
    failed_ambiguous = true,
    cancelled = true,
}

local processable_states = {
    received = true,
    enqueued = true,
    failed_retryable = true,
    processing = true,
    ready_to_deliver = true,
}
"""
)

_OWNERSHIP_LUA = (
    _STATE_LUA
    + r"""
local function owns_job(token, expected_fence, now)
    local expires_at = load_job(now)
    if expires_at == nil then return false, nil end

    local state = redis.call('HGET', KEYS[1], 'state') or ''
    if terminal_states[state] then return false, expires_at end
    if integer(redis.call('HGET', KEYS[1], 'fence'), 0) ~= expected_fence then
        return false, expires_at
    end
    if redis.call('GET', KEYS[2]) ~= token .. ':' .. tostring(expected_fence) then
        return false, expires_at
    end
    if key_expires_at(KEYS[2]) <= now then return false, expires_at end
    return true, expires_at
end
"""
)

_NOTICE_OWNERSHIP_LUA = (
    _BASE_LUA
    + r"""
local function owns_notice(token, expected_fence, now)
    local expires_at = load_job(now)
    if expires_at == nil then return false end
    if redis.call('HGET', KEYS[1], 'state') ~= 'failed' then return false end
    if redis.call('HGET', KEYS[1], 'failure_notice_state') ~= 'pending' then
        return false
    end
    if integer(redis.call('HGET', KEYS[1], 'fence'), 0) ~= expected_fence then
        return false
    end
    if redis.call('GET', KEYS[3]) ~= token .. ':' .. tostring(expected_fence) then
        return false
    end
    return key_expires_at(KEYS[3]) > now
end
"""
)

_CREATE_LUA = (
    _BASE_LUA
    + r"""
local now = integer(ARGV[1], 0)
if load_job(now) ~= nil then return 'existing' end

local expires_at = integer(ARGV[2], 0)
if expires_at <= now then return 'expired' end

local fields = cjson.decode(ARGV[3])
for field, value in pairs(fields) do
    redis.call('HSET', KEYS[1], field, value)
end
redis.call('EXPIREAT', KEYS[1], expires_at)

for index = 4, #KEYS do
    local index_key = KEYS[index]
    redis.call('ZREMRANGEBYSCORE', index_key, '-inf', now)
    redis.call('ZADD', index_key, expires_at, ARGV[4])
    local tail = redis.call('ZRANGE', index_key, -1, -1, 'WITHSCORES')
    if #tail >= 2 then
        redis.call('EXPIREAT', index_key, integer(tail[2], expires_at))
    end
end
return 'created'
"""
)

_CREATE_AUTO_LUA = (
    _BASE_LUA
    + r"""
local now = integer(ARGV[1], 0)
if load_job(now) ~= nil then return 'existing' end
local cooldown = redis.call('GET', KEYS[4])
if cooldown and redis.call('EXPIRETIME', KEYS[4]) > now then
    return 'suppressed'
end
local expires_at = integer(ARGV[2], 0)
if expires_at <= now then return 'expired' end
local fields = cjson.decode(ARGV[3])
for field, value in pairs(fields) do redis.call('HSET', KEYS[1], field, value) end
redis.call('EXPIREAT', KEYS[1], expires_at)
for index = 5, #KEYS do
    local index_key = KEYS[index]
    redis.call('ZREMRANGEBYSCORE', index_key, '-inf', now)
    redis.call('ZADD', index_key, expires_at, ARGV[4])
    local tail = redis.call('ZRANGE', index_key, -1, -1, 'WITHSCORES')
    if #tail >= 2 then redis.call('EXPIREAT', index_key, integer(tail[2], expires_at)) end
end
redis.call('SET', KEYS[4], ARGV[5])
redis.call('EXPIREAT', KEYS[4], now + integer(ARGV[6], 1))
return 'created'
"""
)

_GET_LUA = (
    _BASE_LUA
    + r"""
if load_job(integer(ARGV[1], 0)) == nil then return nil end
return redis.call('HGETALL', KEYS[1])
"""
)

_RECORD_PUBLICATION_LUA = (
    _BASE_LUA
    + r"""
local now = integer(ARGV[1], 0)
if load_job(now) == nil then return 'missing' end

local current = redis.call('HGET', KEYS[1], 'qstash_message_id')
if current and current ~= ARGV[2] then return 'conflict' end

redis.call('HSET', KEYS[1], 'qstash_message_id', ARGV[2])
if not redis.call('HGET', KEYS[1], 'enqueued_at') then
    redis.call('HSET', KEYS[1], 'enqueued_at', tostring(now))
end
redis.call('HSET', KEYS[1], 'updated_at', tostring(now))
if redis.call('HGET', KEYS[1], 'state') == 'received' then
    redis.call('HSET', KEYS[1], 'state', 'enqueued')
end
return 'recorded'
"""
)

_ACQUIRE_LUA = (
    _STATE_LUA
    + r"""
local now = integer(ARGV[1], 0)
local expires_at = load_job(now)
if expires_at == nil then
    return cjson.encode({status = 'missing'})
end

local state = redis.call('HGET', KEYS[1], 'state') or ''
if terminal_states[state] then
    return cjson.encode({status = 'terminal', state = state})
end

local current_lease = redis.call('GET', KEYS[2])
if current_lease then
    local current_expires_at = key_expires_at(KEYS[2])
    if current_expires_at > now then
        local remaining = math.min(current_expires_at, expires_at) - now
        return cjson.encode({status = 'busy', ttl = math.max(remaining, 0)})
    end
    redis.call('DEL', KEYS[2])
end

if not processable_states[state] then
    return cjson.encode({status = 'invalid_state', state = state})
end

local attempts = integer(redis.call('HGET', KEYS[1], 'attempts'), 0)
if attempts >= 4 then
    return cjson.encode({status = 'exhausted', attempts = attempts})
end

local lease_seconds = integer(ARGV[3], 0)
local lease_expires_at = math.min(now + lease_seconds, expires_at)
if lease_expires_at <= now then
    return cjson.encode({status = 'missing'})
end

local fence = integer(redis.call('HGET', KEYS[1], 'fence'), 0) + 1
attempts = attempts + 1
if state == 'received' or state == 'enqueued' or state == 'failed_retryable' then
    state = 'processing'
end
redis.call(
    'HSET',
    KEYS[1],
    'fence', tostring(fence),
    'attempts', tostring(attempts),
    'updated_at', tostring(now),
    'state', state
)
redis.call('SET', KEYS[2], ARGV[2] .. ':' .. tostring(fence))
redis.call('EXPIREAT', KEYS[2], lease_expires_at)
return cjson.encode({
    status = 'acquired',
    fence = fence,
    attempts = attempts,
    state = state,
    ttl = lease_expires_at - now,
})
"""
)

_LEASE_TTL_LUA = (
    _BASE_LUA
    + r"""
local now = integer(ARGV[1], 0)
local expires_at = load_job(now)
if expires_at == nil then return 0 end
if not redis.call('GET', KEYS[2]) then return 0 end

local lease_expires_at = key_expires_at(KEYS[2])
if lease_expires_at <= now then
    redis.call('DEL', KEYS[2])
    return 0
end
return math.max(math.min(lease_expires_at, expires_at) - now, 0)
"""
)

_NOTICE_TTL_LUA = (
    _BASE_LUA
    + r"""
local now = integer(ARGV[1], 0)
local expires_at = load_job(now)
if expires_at == nil then return 0 end
if not redis.call('GET', KEYS[3]) then return 0 end

local lease_expires_at = key_expires_at(KEYS[3])
if lease_expires_at <= now then
    redis.call('DEL', KEYS[3])
    return 0
end
return math.max(math.min(lease_expires_at, expires_at) - now, 0)
"""
)

_RENEW_LUA = (
    _OWNERSHIP_LUA
    + r"""
local now = integer(ARGV[1], 0)
local expected_fence = integer(ARGV[3], -1)
local owned, expires_at = owns_job(ARGV[2], expected_fence, now)
if not owned then return 0 end

local lease_seconds = integer(ARGV[4], 0)
local lease_expires_at = math.min(now + lease_seconds, expires_at)
if lease_expires_at <= now then return 0 end
redis.call('EXPIREAT', KEYS[2], lease_expires_at)
return 1
"""
)

_GUARD_LUA = (
    _OWNERSHIP_LUA
    + r"""
local owned = owns_job(ARGV[2], integer(ARGV[3], -1), integer(ARGV[1], 0))
if owned then return 1 end
return 0
"""
)

_PREPARE_INTENT_LUA = (
    _OWNERSHIP_LUA
    + r"""
local now = integer(ARGV[1], 0)
local fence = integer(ARGV[3], -1)
local owned = owns_job(ARGV[2], fence, now)
if not owned then return cjson.encode({status = 'ownership_lost'}) end

local checkpoint_field = 'checkpoint:' .. ARGV[4]
local checkpoint = redis.call('HGET', KEYS[1], checkpoint_field)
local intent_field = 'intent:' .. ARGV[4]
local existing_raw = redis.call('HGET', KEYS[1], intent_field)
if existing_raw then
    local existing_ok, existing = pcall(cjson.decode, existing_raw)
    local incoming_ok, incoming = pcall(cjson.decode, ARGV[5])
    if not existing_ok or not incoming_ok
        or type(existing) ~= 'table' or type(incoming) ~= 'table'
        or existing['kind'] ~= incoming['kind']
        or existing['chunk_index'] ~= incoming['chunk_index']
        or existing['payload_hash'] ~= incoming['payload_hash'] then
        return cjson.encode({status = 'conflict'})
    end
    if checkpoint then
        return cjson.encode({status = 'checkpointed', checkpoint = checkpoint})
    end
    if ARGV[6] == '1' and existing['fence'] ~= fence then
        return cjson.encode({status = 'ambiguous'})
    end
    return cjson.encode({status = 'prepared'})
end

if checkpoint then
    return cjson.encode({status = 'conflict'})
end

redis.call('HSET', KEYS[1], intent_field, ARGV[5])
redis.call('HSET', KEYS[1], 'updated_at', tostring(now))
return cjson.encode({status = 'prepared'})
"""
)

_CHECKPOINT_LUA = (
    _OWNERSHIP_LUA
    + r"""
local now = integer(ARGV[1], 0)
local fence = integer(ARGV[3], -1)
local owned = owns_job(ARGV[2], fence, now)
if not owned then return 'ownership_lost' end

local intent_field = 'intent:' .. ARGV[4]
if redis.call('HEXISTS', KEYS[1], intent_field) == 0 then return 'intent_missing' end

local checkpoint_field = 'checkpoint:' .. ARGV[4]
local current = redis.call('HGET', KEYS[1], checkpoint_field)
if current and current ~= ARGV[5] then return 'conflict' end
redis.call('HSET', KEYS[1], checkpoint_field, ARGV[5])

if ARGV[4] == 'placeholder' then
    local decoded_ok, decoded = pcall(cjson.decode, ARGV[5])
    if decoded_ok and type(decoded) == 'table' then
        local message_id = decoded['message_id']
        if type(message_id) == 'number' and message_id % 1 == 0 then
            redis.call('HSET', KEYS[1], 'placeholder_message_id', tostring(message_id))
        end
    end
end
redis.call('HSET', KEYS[1], 'updated_at', tostring(now))
return 'checkpointed'
"""
)

_CLEAR_INTENT_LUA = (
    _OWNERSHIP_LUA
    + r"""
local now = integer(ARGV[1], 0)
local owned = owns_job(ARGV[2], integer(ARGV[3], -1), now)
if not owned then return 'ownership_lost' end
if redis.call('HEXISTS', KEYS[1], 'checkpoint:' .. ARGV[4]) == 1 then
    return 'checkpointed'
end
redis.call('HDEL', KEYS[1], 'intent:' .. ARGV[4])
redis.call('HSET', KEYS[1], 'updated_at', tostring(now))
return 'cleared'
"""
)

_SAVE_ANSWER_LUA = (
    _OWNERSHIP_LUA
    + r"""
local now = integer(ARGV[1], 0)
local owned = owns_job(ARGV[2], integer(ARGV[3], -1), now)
if not owned then return cjson.encode({status = 'ownership_lost'}) end

local state = redis.call('HGET', KEYS[1], 'state') or ''
if state ~= 'processing' and state ~= 'ready_to_deliver' then
    return cjson.encode({status = 'state_rejected'})
end

local current = redis.call('HGET', KEYS[1], 'answer_text')
if current then
    if state == 'processing' then
        redis.call('HSET', KEYS[1], 'state', 'ready_to_deliver', 'updated_at', tostring(now))
    end
    return cjson.encode({status = 'existing', answer = current})
end
redis.call(
    'HSET',
    KEYS[1],
    'answer_text', ARGV[4],
    'answer_sha256', ARGV[5],
    'answer_saved_at', tostring(now),
    'state', 'ready_to_deliver',
    'updated_at', tostring(now)
)
return cjson.encode({status = 'saved', answer = ARGV[4]})
"""
)

_FINISH_OWNED_LUA = (
    _OWNERSHIP_LUA
    + r"""
local now = integer(ARGV[1], 0)
local fence = integer(ARGV[3], -1)
local owned = owns_job(ARGV[2], fence, now)
if not owned then return 'ownership_lost' end

local state = redis.call('HGET', KEYS[1], 'state') or ''
local target = ARGV[4]
local allowed_transitions = {
    processing = {
        failed_retryable = true,
        failed = true,
        failed_ambiguous = true,
    },
    ready_to_deliver = {
        delivered = true,
        failed_retryable = true,
        failed = true,
        failed_ambiguous = true,
    },
}
if not allowed_transitions[state] or not allowed_transitions[state][target] then
    return 'state_rejected'
end

redis.call('HSET', KEYS[1], 'state', target, 'updated_at', tostring(now))
if ARGV[5] ~= '' then
    redis.call('HSET', KEYS[1], 'error_class', ARGV[5])
else
    redis.call('HDEL', KEYS[1], 'error_class')
end

if terminal_states[target] then
    redis.call('HSET', KEYS[1], 'fence', tostring(fence + 1))
    if target == 'failed' and ARGV[6] ~= '' and ARGV[7] ~= '' then
        redis.call(
            'HSET',
            KEYS[1],
            'failure_notice_hash', ARGV[6],
            'failure_notice_text', ARGV[7]
        )
        local placeholder = redis.call('HGET', KEYS[1], 'placeholder_message_id')
        local notice_state = 'none'
        if placeholder and placeholder ~= '' then notice_state = 'pending' end
        redis.call('HSET', KEYS[1], 'failure_notice_state', notice_state)
    end
end
redis.call('DEL', KEYS[2])
return 'finished'
"""
)

_RELEASE_LUA = (
    _BASE_LUA
    + r"""
local now = integer(ARGV[1], 0)
load_job(now)
if redis.call('GET', KEYS[2]) ~= ARGV[2] .. ':' .. ARGV[3] then return 0 end
redis.call('DEL', KEYS[2])
return 1
"""
)

_FAILURE_TAKEOVER_LUA = (
    _STATE_LUA
    + r"""
local now = integer(ARGV[1], 0)
local expires_at = load_job(now)
if expires_at == nil then return cjson.encode({status = 'missing'}) end

local saved_message_id = redis.call('HGET', KEYS[1], 'qstash_message_id')
if not saved_message_id then
    return cjson.encode({status = 'metadata_pending'})
end
if saved_message_id ~= ARGV[2] then
    return cjson.encode({status = 'mismatch'})
end
if integer(redis.call('HGET', KEYS[1], 'qstash_max_retries'), -1)
    ~= integer(ARGV[5], -2) then
    return cjson.encode({status = 'mismatch'})
end

local current_lease = redis.call('GET', KEYS[2])
if current_lease then
    local current_expires_at = key_expires_at(KEYS[2])
    if current_expires_at > now then
        local remaining = math.min(current_expires_at, expires_at) - now
        return cjson.encode({status = 'busy', ttl = math.max(remaining, 0)})
    end
    redis.call('DEL', KEYS[2])
end

local state = redis.call('HGET', KEYS[1], 'state') or ''
if terminal_states[state] then
    return cjson.encode({status = 'terminal', state = state})
end

local fence = integer(redis.call('HGET', KEYS[1], 'fence'), 0) + 1
local placeholder = redis.call('HGET', KEYS[1], 'placeholder_message_id')
local notice_state = 'none'
if placeholder and placeholder ~= '' then notice_state = 'pending' end
redis.call(
    'HSET',
    KEYS[1],
    'fence', tostring(fence),
    'state', 'failed',
    'error_class', 'qstash_retries_exhausted',
    'updated_at', tostring(now),
    'failure_notice_hash', ARGV[3],
    'failure_notice_text', ARGV[4],
    'failure_notice_state', notice_state
)
redis.call('DEL', KEYS[2])
return cjson.encode({status = 'failed', fence = fence})
"""
)

_CLAIM_FAILURE_NOTICE_LUA = (
    _BASE_LUA
    + r"""
local now = integer(ARGV[1], 0)
local expires_at = load_job(now)
if expires_at == nil then return cjson.encode({status = 'missing'}) end
if redis.call('HGET', KEYS[1], 'state') ~= 'failed' then
    return cjson.encode({status = 'terminal'})
end

local notice_state = redis.call('HGET', KEYS[1], 'failure_notice_state') or 'none'
if notice_state ~= 'pending' then
    return cjson.encode({status = notice_state})
end

local placeholder = integer(
    redis.call('HGET', KEYS[1], 'placeholder_message_id'),
    0
)
if placeholder <= 0 then
    redis.call('HSET', KEYS[1], 'failure_notice_state', 'none')
    return cjson.encode({status = 'none'})
end

local current_lease = redis.call('GET', KEYS[3])
if current_lease then
    local current_expires_at = key_expires_at(KEYS[3])
    if current_expires_at > now then
        local remaining = math.min(current_expires_at, expires_at) - now
        return cjson.encode({status = 'busy', ttl = math.max(remaining, 0)})
    end
    redis.call('DEL', KEYS[3])
end

local lease_seconds = integer(ARGV[3], 0)
local lease_expires_at = math.min(now + lease_seconds, expires_at)
if lease_expires_at <= now then return cjson.encode({status = 'missing'}) end

local fence = integer(redis.call('HGET', KEYS[1], 'fence'), 0)
redis.call('SET', KEYS[3], ARGV[2] .. ':' .. tostring(fence))
redis.call('EXPIREAT', KEYS[3], lease_expires_at)
local result = {
    status = 'claimed',
    fence = fence,
    placeholder_message_id = placeholder,
}
local failure_notice_hash = redis.call('HGET', KEYS[1], 'failure_notice_hash')
if failure_notice_hash then result['failure_notice_hash'] = failure_notice_hash end
local failure_notice_text = redis.call('HGET', KEYS[1], 'failure_notice_text')
if failure_notice_text then result['failure_notice_text'] = failure_notice_text end
return cjson.encode(result)
"""
)

_GUARD_FAILURE_NOTICE_LUA = (
    _NOTICE_OWNERSHIP_LUA
    + r"""
if owns_notice(ARGV[2], integer(ARGV[3], -1), integer(ARGV[1], 0)) then
    return 1
end
return 0
"""
)

_COMPLETE_FAILURE_NOTICE_LUA = (
    _NOTICE_OWNERSHIP_LUA
    + r"""
local now = integer(ARGV[1], 0)
if not owns_notice(ARGV[2], integer(ARGV[3], -1), now) then
    return 'ownership_lost'
end
redis.call(
    'HSET',
    KEYS[1],
    'checkpoint:failure_notice', ARGV[4],
    'failure_notice_state', 'delivered',
    'updated_at', tostring(now)
)
redis.call('DEL', KEYS[3])
return 'completed'
"""
)

_FAIL_FAILURE_NOTICE_LUA = (
    _NOTICE_OWNERSHIP_LUA
    + r"""
local now = integer(ARGV[1], 0)
if not owns_notice(ARGV[2], integer(ARGV[3], -1), now) then
    return 'ownership_lost'
end
redis.call(
    'HSET',
    KEYS[1],
    'failure_notice_state', 'failed_permanent',
    'failure_notice_error', ARGV[4],
    'updated_at', tostring(now)
)
redis.call('DEL', KEYS[3])
return 'completed'
"""
)

_RELEASE_FAILURE_NOTICE_LUA = (
    _BASE_LUA
    + r"""
local now = integer(ARGV[1], 0)
load_job(now)
if redis.call('GET', KEYS[3]) ~= ARGV[2] .. ':' .. ARGV[3] then return 0 end
redis.call('DEL', KEYS[3])
return 1
"""
)

_INDEX_MEMBERS_LUA = r"""
local now = tonumber(ARGV[1]) or 0
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now)
if redis.call('ZCARD', KEYS[1]) == 0 then
    redis.call('DEL', KEYS[1])
    return {}
end
return redis.call('ZRANGE', KEYS[1], 0, -1)
"""

_INDEX_TTL_LUA = r"""
local now = tonumber(ARGV[1]) or 0
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now)
local tail = redis.call('ZRANGE', KEYS[1], -1, -1, 'WITHSCORES')
if #tail < 2 then
    redis.call('DEL', KEYS[1])
    return 0
end
local expires_at = tonumber(tail[2]) or 0
if expires_at <= now then
    redis.call('DEL', KEYS[1])
    return 0
end
redis.call('EXPIREAT', KEYS[1], expires_at)
return expires_at - now
"""

_PURGE_LUA = r"""
if redis.call('EXISTS', KEYS[1]) == 0 then return nil end
local snapshot = redis.call('HGETALL', KEYS[1])
local has_receipt = false
for index = 1, #snapshot, 2 do
    local name = snapshot[index]
    if string.sub(name, 1, 11) == 'checkpoint:' then
        local ok, checkpoint = pcall(cjson.decode, snapshot[index + 1])
        if ok and type(checkpoint) == 'table' and type(checkpoint['message_id']) == 'number' and checkpoint['message_id'] > 0 then
            redis.call('SADD', KEYS[4], tostring(checkpoint['message_id']))
            has_receipt = true
        end
    end
end
if has_receipt then redis.call('EXPIRE', KEYS[4], ARGV[2]) end
local fence = tonumber(redis.call('HGET', KEYS[1], 'fence') or '0') + 1
redis.call('HSET', KEYS[1], 'state', 'cancelled', 'fence', tostring(fence))
redis.call('DEL', KEYS[1], KEYS[2], KEYS[3])
for index = 5, #KEYS do
    redis.call('ZREM', KEYS[index], ARGV[1])
    if redis.call('ZCARD', KEYS[index]) == 0 then redis.call('DEL', KEYS[index]) end
end
return snapshot
"""

_JOB_TTL_LUA = (
    _BASE_LUA
    + r"""
local now = integer(ARGV[1], 0)
local expires_at = load_job(now)
if expires_at == nil then return 0 end
return expires_at - now
"""
)


def _job_key(job_id: str) -> str:
    return f"job:{job_id}"


def _lease_key(job_id: str) -> str:
    return f"{_job_key(job_id)}:lease"


def _failure_lease_key(job_id: str) -> str:
    return f"{_job_key(job_id)}:failure-lease"


def _job_keys(job_id: str) -> list[str]:
    return [_job_key(job_id), _lease_key(job_id), _failure_lease_key(job_id)]


def _text(value: object) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8")
    if isinstance(value, str):
        return value
    raise RuntimeError("invalid Redis script response")


def _result_object(value: object) -> dict[str, object]:
    if isinstance(value, Mapping):
        return {str(key): item for key, item in value.items()}
    try:
        decoded = json.loads(_text(value))
    except (TypeError, ValueError) as exc:
        raise RuntimeError("invalid Redis script response") from exc
    if not isinstance(decoded, dict):
        raise RuntimeError("invalid Redis script response")
    return decoded


def _result_int(value: object) -> int:
    if isinstance(value, bool):
        return int(value)
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise RuntimeError("invalid Redis script response") from exc


def _result_bool(value: object) -> bool:
    return _result_int(value) == 1


def _hash_result(value: object) -> dict[str, str] | None:
    if value is None:
        return None
    if isinstance(value, Mapping):
        return {str(key): str(item) for key, item in value.items()}
    if not isinstance(value, (list, tuple)) or len(value) % 2:
        raise RuntimeError("invalid Redis script response")
    result: dict[str, str] = {}
    for index in range(0, len(value), 2):
        result[_text(value[index])] = _text(value[index + 1])
    return result


class UpstashJobBackend:
    """Synchronous Upstash Redis implementation of the job backend protocol."""

    def __init__(self, url: str, token: str) -> None:
        self._r = build_upstash_redis(url, token)
        self._lock = threading.RLock()

    def _eval(
        self,
        script: str,
        *,
        keys: Sequence[str],
        args: Sequence[str],
    ) -> object:
        if not self._lock.acquire(timeout=UPSTASH_LOCK_TIMEOUT_SECONDS):
            raise TimeoutError("Redis client contention")
        try:
            return self._r.eval(script, keys=list(keys), args=list(args))
        finally:
            self._lock.release()

    def _eval_job(self, script: str, job_id: str, args: Sequence[str]) -> object:
        return self._eval(script, keys=_job_keys(job_id), args=args)

    def create(
        self,
        *,
        job_id: str,
        fields: Mapping[str, str],
        index_keys: Sequence[str],
        now: int,
    ) -> str:
        try:
            expires_at = int(fields.get("expires_at", "0"))
        except (TypeError, ValueError):
            expires_at = 0
        indexes = sorted(set(index_keys))
        result = self._eval(
            _CREATE_LUA,
            keys=[*_job_keys(job_id), *indexes],
            args=[
                str(now),
                str(expires_at),
                json.dumps(dict(fields), ensure_ascii=False, separators=(",", ":")),
                job_id,
            ],
        )
        return _text(result)

    def create_auto(
        self,
        *,
        job_id: str,
        fields: Mapping[str, str],
        index_keys: Sequence[str],
        cooldown_key: str,
        cooldown_owner: str,
        cooldown_seconds: int,
        now: int,
    ) -> str:
        expires_at = int(fields.get("expires_at", "0"))
        result = self._eval(
            _CREATE_AUTO_LUA,
            keys=[*_job_keys(job_id), cooldown_key, *sorted(set(index_keys))],
            args=[
                str(now),
                str(expires_at),
                json.dumps(dict(fields), ensure_ascii=False, separators=(",", ":")),
                job_id,
                cooldown_owner,
                str(cooldown_seconds),
            ],
        )
        return _text(result)

    def get(self, job_id: str, *, now: int) -> dict[str, str] | None:
        return _hash_result(self._eval_job(_GET_LUA, job_id, [str(now)]))

    def record_publication(self, job_id: str, message_id: str, *, now: int) -> str:
        result = self._eval_job(
            _RECORD_PUBLICATION_LUA,
            job_id,
            [str(now), message_id],
        )
        return _text(result)

    def acquire(
        self,
        job_id: str,
        *,
        token: str,
        lease_seconds: int,
        now: int,
    ) -> dict[str, object]:
        return _result_object(
            self._eval_job(
                _ACQUIRE_LUA,
                job_id,
                [str(now), token, str(lease_seconds)],
            )
        )

    def lease_ttl(self, job_id: str, *, now: int) -> int:
        return _result_int(self._eval_job(_LEASE_TTL_LUA, job_id, [str(now)]))

    def renew(
        self,
        job_id: str,
        *,
        token: str,
        fence: int,
        lease_seconds: int,
        now: int,
    ) -> bool:
        return _result_bool(
            self._eval_job(
                _RENEW_LUA,
                job_id,
                [str(now), token, str(fence), str(lease_seconds)],
            )
        )

    def guard(self, job_id: str, *, token: str, fence: int, now: int) -> bool:
        return _result_bool(
            self._eval_job(
                _GUARD_LUA,
                job_id,
                [str(now), token, str(fence)],
            )
        )

    def prepare_intent(
        self,
        job_id: str,
        *,
        token: str,
        fence: int,
        name: str,
        intent_json: str,
        ambiguous_on_takeover: bool,
        now: int,
    ) -> dict[str, object]:
        return _result_object(
            self._eval_job(
                _PREPARE_INTENT_LUA,
                job_id,
                [
                    str(now),
                    token,
                    str(fence),
                    name,
                    intent_json,
                    "1" if ambiguous_on_takeover else "0",
                ],
            )
        )

    def checkpoint(
        self,
        job_id: str,
        *,
        token: str,
        fence: int,
        name: str,
        checkpoint_json: str,
        now: int,
    ) -> str:
        result = self._eval_job(
            _CHECKPOINT_LUA,
            job_id,
            [str(now), token, str(fence), name, checkpoint_json],
        )
        return _text(result)

    def clear_intent(
        self,
        job_id: str,
        *,
        token: str,
        fence: int,
        name: str,
        now: int,
    ) -> str:
        result = self._eval_job(
            _CLEAR_INTENT_LUA,
            job_id,
            [str(now), token, str(fence), name],
        )
        return _text(result)

    def save_answer(
        self,
        job_id: str,
        *,
        token: str,
        fence: int,
        answer: str,
        answer_hash: str,
        now: int,
    ) -> dict[str, object]:
        return _result_object(
            self._eval_job(
                _SAVE_ANSWER_LUA,
                job_id,
                [str(now), token, str(fence), answer, answer_hash],
            )
        )

    def finish_owned(
        self,
        job_id: str,
        *,
        token: str,
        fence: int,
        target_state: str,
        error_class: str | None,
        failure_notice_hash: str | None,
        failure_notice_text: str | None,
        now: int,
    ) -> str:
        result = self._eval_job(
            _FINISH_OWNED_LUA,
            job_id,
            [
                str(now),
                token,
                str(fence),
                target_state,
                error_class or "",
                failure_notice_hash or "",
                failure_notice_text or "",
            ],
        )
        return _text(result)

    def release(self, job_id: str, *, token: str, fence: int, now: int) -> bool:
        return _result_bool(
            self._eval_job(
                _RELEASE_LUA,
                job_id,
                [str(now), token, str(fence)],
            )
        )

    def failure_takeover(
        self,
        job_id: str,
        *,
        source_message_id: str,
        failure_notice_hash: str,
        failure_notice_text: str,
        max_retries: int,
        now: int,
    ) -> dict[str, object]:
        return _result_object(
            self._eval_job(
                _FAILURE_TAKEOVER_LUA,
                job_id,
                [
                    str(now),
                    source_message_id,
                    failure_notice_hash,
                    failure_notice_text,
                    str(max_retries),
                ],
            )
        )

    def claim_failure_notice(
        self,
        job_id: str,
        *,
        token: str,
        lease_seconds: int,
        now: int,
    ) -> dict[str, object]:
        return _result_object(
            self._eval_job(
                _CLAIM_FAILURE_NOTICE_LUA,
                job_id,
                [str(now), token, str(lease_seconds)],
            )
        )

    def guard_failure_notice(
        self,
        job_id: str,
        *,
        token: str,
        fence: int,
        now: int,
    ) -> bool:
        return _result_bool(
            self._eval_job(
                _GUARD_FAILURE_NOTICE_LUA,
                job_id,
                [str(now), token, str(fence)],
            )
        )

    def complete_failure_notice(
        self,
        job_id: str,
        *,
        token: str,
        fence: int,
        checkpoint_json: str,
        now: int,
    ) -> str:
        result = self._eval_job(
            _COMPLETE_FAILURE_NOTICE_LUA,
            job_id,
            [str(now), token, str(fence), checkpoint_json],
        )
        return _text(result)

    def fail_failure_notice(
        self,
        job_id: str,
        *,
        token: str,
        fence: int,
        error_class: str,
        now: int,
    ) -> str:
        result = self._eval_job(
            _FAIL_FAILURE_NOTICE_LUA,
            job_id,
            [str(now), token, str(fence), error_class],
        )
        return _text(result)

    def release_failure_notice(
        self,
        job_id: str,
        *,
        token: str,
        fence: int,
        now: int,
    ) -> bool:
        return _result_bool(
            self._eval_job(
                _RELEASE_FAILURE_NOTICE_LUA,
                job_id,
                [str(now), token, str(fence)],
            )
        )

    def index_members(self, index_key: str, *, now: int) -> list[str]:
        result = self._eval(
            _INDEX_MEMBERS_LUA,
            keys=[index_key],
            args=[str(now)],
        )
        if not isinstance(result, (list, tuple)):
            raise RuntimeError("invalid Redis script response")
        return [_text(item) for item in result]

    def purge(
        self,
        job_id: str,
        *,
        index_keys: Sequence[str],
        receipt_key: str,
        receipt_ttl: int,
        now: int,
    ) -> dict[str, str] | None:
        return _hash_result(
            self._eval(
                _PURGE_LUA,
                keys=[
                    *_job_keys(job_id),
                    receipt_key,
                    *sorted(set(index_keys)),
                ],
                args=[job_id, str(receipt_ttl)],
            )
        )

    def ttl(self, key: str, *, now: int) -> int:
        failure_suffix = ":failure-lease"
        lease_suffix = ":lease"
        if key.startswith("job:") and key.endswith(failure_suffix):
            job_id = key[len("job:") : -len(failure_suffix)]
            return _result_int(self._eval_job(_NOTICE_TTL_LUA, job_id, [str(now)]))
        if key.startswith("job:") and key.endswith(lease_suffix):
            job_id = key[len("job:") : -len(lease_suffix)]
            return self.lease_ttl(job_id, now=now)
        if key.startswith("job:"):
            job_id = key[len("job:") :]
            return _result_int(self._eval_job(_JOB_TTL_LUA, job_id, [str(now)]))
        return _result_int(self._eval(_INDEX_TTL_LUA, keys=[key], args=[str(now)]))
