"""Key-value store on top of Upstash Redis (REST).

We abstract exactly the operations the bot needs behind a thin ``KV`` interface.
There are two implementations:

* ``UpstashKV`` — production: Upstash Redis over HTTP/REST (no connection pools —
  ideal for serverless).
* ``MemoryKV``  — local/tests: in-memory. NOTE: on serverless, memory does not
  survive between function invocations, so MemoryKV is unsuitable in production
  (dedup/history would be lost). Backend selection is automatic: if Upstash
  credentials are set we use Upstash, otherwise Memory.
"""

from __future__ import annotations

import json
import threading
import time
from typing import Optional, Protocol

import httpx

from app.settings import settings


UPSTASH_REDIS_TIMEOUT = httpx.Timeout(
    connect=3.0,
    read=12.0,
    write=5.0,
    pool=3.0,
)
UPSTASH_LOCK_TIMEOUT_SECONDS = 2.0


def build_upstash_redis(url: str, token: str):
    """Create the pinned SDK client with bounded transport behavior.

    Upstash Redis 1.7.0 exposes no public timeout argument and otherwise creates
    an ``httpx.Client(timeout=None)``. Replacing that freshly-created, unused
    transport is the narrow compatibility shim until the SDK exposes a timeout
    option. Disabling SDK retries keeps a Redis operation inside one bounded
    request instead of its fixed three-second retry sleep.
    """
    from upstash_redis import Redis

    client = Redis(
        url=url,
        token=token,
        rest_retries=0,
        read_your_writes=True,
    )
    previous_transport = client._http._client
    client._http._client = httpx.Client(timeout=UPSTASH_REDIS_TIMEOUT)
    previous_transport.close()
    return client


class KV(Protocol):
    """Minimal store interface used by the bot."""

    def ping(self) -> bool: ...
    def get(self, key: str) -> Optional[str]: ...
    def set(self, key: str, value: str, ex: Optional[int] = None) -> None: ...
    def set_nx(self, key: str, value: str, ex: Optional[int] = None) -> bool: ...
    def lpush(self, key: str, value: str) -> int: ...
    def ltrim(self, key: str, start: int, stop: int) -> None: ...
    def lrange(self, key: str, start: int, stop: int) -> list[str]: ...
    def llen(self, key: str) -> int: ...
    def list_upsert_json(
        self,
        key: str,
        identity_field: str,
        identity_value: str,
        value: str,
        limit: int,
        ex: Optional[int] = None,
        prune_field: Optional[str] = None,
        min_value: Optional[int] = None,
        block_key: Optional[str] = None,
    ) -> int: ...
    def list_prune_json(self, key: str, field: str, min_value: int) -> int: ...
    def observe_user_json(
        self, user_id: int, normalized_username: Optional[str], value: str
    ) -> str: ...
    def sadd(self, key: str, *members: str) -> int: ...
    def sismember(self, key: str, member: str) -> bool: ...
    def smembers(self, key: str) -> set[str]: ...
    def set_add_expiring(self, key: str, members: set[str], ex: int) -> set[str]: ...
    def srem(self, key: str, *members: str) -> int: ...
    def delete_if_value(self, key: str, expected: str) -> bool: ...
    def increment(self, key: str) -> int: ...
    def admin_role_change(self, user_id: int, *, add: bool) -> bool: ...
    def rate_limit(self, key: str, limit: int, window_seconds: int) -> bool: ...
    def indexed_json_put(
        self,
        index_key: str,
        item_key: str,
        member: str,
        value: str,
        *,
        create_only: bool,
    ) -> str: ...
    def indexed_delete(self, index_key: str, item_key: str, member: str) -> bool: ...
    def rename_indexed_set(
        self,
        index_key: str,
        old_item_key: str,
        new_item_key: str,
        old_members_key: str,
        new_members_key: str,
        old_member: str,
        new_member: str,
        value: str,
    ) -> str: ...
    def list_member_add(self, meta_key: str, members_key: str, member: str) -> str: ...
    def list_delete(
        self, index_key: str, meta_key: str, members_key: str, member: str
    ) -> bool: ...
    def delete_user_alias(self, user_id: int) -> bool: ...
    def delete_user_data(self, user_id: int) -> tuple[bool, int]: ...
    def list_privacy_filter(
        self,
        key: str,
        user_id: int,
        outbound_message_ids: set[int],
    ) -> int: ...
    def list_remove_message_ids(self, key: str, message_ids: set[int]) -> int: ...
    def delete(self, *keys: str) -> int: ...
    def backend(self) -> str: ...


def _redis_slice(lst: list[str], start: int, stop: int) -> list[str]:
    """Mirror Redis LRANGE/LTRIM semantics (bounds inclusive, negative indices
    counted from the end)."""
    n = len(lst)
    if n == 0:
        return []
    if start < 0:
        start = max(n + start, 0)
    if stop < 0:
        stop = n + stop
    stop = min(stop, n - 1)
    if start > stop or start >= n:
        return []
    return lst[start : stop + 1]


def _json_int(record: dict, field: str, default: int = -1) -> int:
    value = record.get(field)
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return default


def _record_version(record: dict) -> tuple[int, int, int]:
    """Order retries/edits of one Telegram message without trusting arrival order."""
    timestamp = _json_int(record, "ts")
    edit_timestamp = _json_int(record, "edit_ts")
    is_edited = edit_timestamp >= 0 or record.get("is_edited") is True
    effective_timestamp = edit_timestamp if edit_timestamp >= 0 else timestamp
    return (
        effective_timestamp,
        int(is_edited),
        _json_int(record, "source_update_id"),
    )


def _record_position(record: dict) -> tuple[int, int]:
    """Chronological position in the capped Telegram history buffer."""
    return _json_int(record, "ts"), _json_int(record, "message_id")


class MemoryKV:
    """Thread-safe in-memory store for local development and tests."""

    def __init__(self) -> None:
        self._values: dict[str, str] = {}
        self._expiry: dict[str, float] = {}
        self._lists: dict[str, list[str]] = {}
        self._sets: dict[str, set[str]] = {}
        self._lock = threading.RLock()

    def _purge_if_expired(self, key: str) -> None:
        exp = self._expiry.get(key)
        if exp is not None and exp <= time.time():
            self._values.pop(key, None)
            self._lists.pop(key, None)
            self._sets.pop(key, None)
            self._expiry.pop(key, None)

    def ping(self) -> bool:
        return True

    def _set_unlocked(self, key: str, value: str, ex: Optional[int]) -> None:
        self._values[key] = value
        if ex is not None:
            self._expiry[key] = time.time() + ex
        else:
            self._expiry.pop(key, None)

    def get(self, key: str) -> Optional[str]:
        with self._lock:
            self._purge_if_expired(key)
            return self._values.get(key)

    def set(self, key: str, value: str, ex: Optional[int] = None) -> None:
        with self._lock:
            self._set_unlocked(key, value, ex)

    def set_nx(self, key: str, value: str, ex: Optional[int] = None) -> bool:
        with self._lock:
            self._purge_if_expired(key)
            if key in self._values:
                return False
            self._set_unlocked(key, value, ex)
            return True

    def lpush(self, key: str, value: str) -> int:
        with self._lock:
            self._purge_if_expired(key)
            lst = self._lists.setdefault(key, [])
            lst.insert(0, value)
            return len(lst)

    def ltrim(self, key: str, start: int, stop: int) -> None:
        with self._lock:
            self._purge_if_expired(key)
            self._lists[key] = _redis_slice(self._lists.get(key, []), start, stop)

    def lrange(self, key: str, start: int, stop: int) -> list[str]:
        with self._lock:
            self._purge_if_expired(key)
            return list(_redis_slice(self._lists.get(key, []), start, stop))

    def llen(self, key: str) -> int:
        with self._lock:
            self._purge_if_expired(key)
            return len(self._lists.get(key, []))

    def list_upsert_json(
        self,
        key: str,
        identity_field: str,
        identity_value: str,
        value: str,
        limit: int,
        ex: Optional[int] = None,
        prune_field: Optional[str] = None,
        min_value: Optional[int] = None,
        block_key: Optional[str] = None,
    ) -> int:
        """Atomically keep the newest version and newest chronological items."""
        if limit <= 0:
            raise ValueError("limit must be positive")
        if ex is not None and ex <= 0:
            raise ValueError("ex must be positive")
        with self._lock:
            self._purge_if_expired(key)
            if block_key is not None:
                self._purge_if_expired(block_key)
                if block_key in self._values:
                    return len(self._lists.get(key, []))
            items = self._lists.setdefault(key, [])
            try:
                incoming = json.loads(value)
            except (TypeError, ValueError) as exc:
                raise ValueError("value must be a JSON object") from exc
            if not isinstance(incoming, dict):
                raise ValueError("value must be a JSON object")

            selected_value = value
            selected_record = incoming
            remaining: list[tuple[str, Optional[dict], int]] = []
            for index, item in enumerate(items):
                try:
                    decoded = json.loads(item)
                except (TypeError, ValueError):
                    decoded = None
                if (
                    isinstance(decoded, dict)
                    and str(decoded.get(identity_field)) == identity_value
                ):
                    if _record_version(decoded) > _record_version(selected_record):
                        selected_value = item
                        selected_record = decoded
                    continue
                remaining.append(
                    (item, decoded if isinstance(decoded, dict) else None, index)
                )

            remaining.append((selected_value, selected_record, len(items)))
            kept: list[tuple[str, Optional[dict], int]] = []
            for item, decoded, original_index in remaining:
                if prune_field is not None and min_value is not None:
                    raw_timestamp = (
                        decoded.get(prune_field) if isinstance(decoded, dict) else None
                    )
                    timestamp = (
                        raw_timestamp
                        if isinstance(raw_timestamp, int)
                        and not isinstance(raw_timestamp, bool)
                        else None
                    )
                    if timestamp is None or timestamp < min_value:
                        continue
                kept.append((item, decoded, original_index))

            # Valid JSON records sort by Telegram chronology. Corrupt legacy
            # values are retained only when no prune contract was requested and
            # sort behind valid records until a history read/write removes them.
            kept.sort(
                key=lambda entry: (
                    1 if entry[1] is not None else 0,
                    *(_record_position(entry[1]) if entry[1] is not None else (-1, -1)),
                    -entry[2],
                ),
                reverse=True,
            )
            items[:] = [item for item, _decoded, _index in kept[:limit]]
            if not items:
                self._lists.pop(key, None)
                self._expiry.pop(key, None)
            elif ex is not None:
                self._expiry[key] = time.time() + ex
            return len(items)

    def list_prune_json(self, key: str, field: str, min_value: int) -> int:
        """Atomically remove malformed/older JSON items without refreshing TTL."""
        with self._lock:
            self._purge_if_expired(key)
            items = self._lists.get(key, [])
            kept: list[str] = []
            for item in items:
                try:
                    decoded = json.loads(item)
                except (TypeError, ValueError):
                    continue
                raw_value = decoded.get(field) if isinstance(decoded, dict) else None
                value = (
                    raw_value
                    if isinstance(raw_value, int) and not isinstance(raw_value, bool)
                    else None
                )
                if value is not None and value >= min_value:
                    kept.append(item)
            if kept:
                self._lists[key] = kept
            else:
                self._lists.pop(key, None)
                self._expiry.pop(key, None)
            return len(kept)

    def observe_user_json(
        self, user_id: int, normalized_username: Optional[str], value: str
    ) -> str:
        """Atomically update a profile and the globally-owned username alias."""
        try:
            incoming = json.loads(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("user value must be a JSON object") from exc
        if not isinstance(incoming, dict):
            raise ValueError("user value must be a JSON object")

        def order(record: dict) -> tuple[int, int]:
            return _json_int(record, "last_seen_at"), _json_int(
                record, "last_update_id"
            )

        def normalized(record: dict) -> Optional[str]:
            value = record.get("username")
            if not isinstance(value, str):
                return None
            result = value.strip().lstrip("@").casefold()
            return result or None

        profile_key = f"user:{user_id}"
        with self._lock:
            self._purge_if_expired(profile_key)
            current_raw = self._values.get(profile_key)
            try:
                current = json.loads(current_raw) if current_raw is not None else None
            except (TypeError, ValueError):
                current = None
            if not isinstance(current, dict):
                current = None

            if current is not None and order(current) > order(incoming):
                return current_raw or value

            desired_username = normalized_username
            if desired_username:
                alias_key = f"username:{desired_username}"
                self._purge_if_expired(alias_key)
                owner_raw = self._values.get(alias_key)
                try:
                    owner_id = int(owner_raw) if owner_raw is not None else None
                except (TypeError, ValueError):
                    owner_id = None
                if owner_id is not None and owner_id != user_id:
                    owner_key = f"user:{owner_id}"
                    self._purge_if_expired(owner_key)
                    owner_raw_profile = self._values.get(owner_key)
                    try:
                        owner = (
                            json.loads(owner_raw_profile)
                            if owner_raw_profile is not None
                            else None
                        )
                    except (TypeError, ValueError):
                        owner = None
                    if isinstance(owner, dict) and order(owner) >= order(incoming):
                        desired_username = None
                    else:
                        if (
                            isinstance(owner, dict)
                            and normalized(owner) == normalized_username
                        ):
                            owner["username"] = None
                            self._set_unlocked(
                                owner_key,
                                json.dumps(
                                    owner,
                                    ensure_ascii=False,
                                    separators=(",", ":"),
                                ),
                                None,
                            )
                        self._set_unlocked(alias_key, str(user_id), None)

            old_username = normalized(current) if current is not None else None
            if old_username and old_username != desired_username:
                old_alias_key = f"username:{old_username}"
                self._purge_if_expired(old_alias_key)
                if self._values.get(old_alias_key) == str(user_id):
                    self._values.pop(old_alias_key, None)
                    self._expiry.pop(old_alias_key, None)

            stored = dict(incoming)
            stored["username"] = (
                incoming.get("username") if desired_username is not None else None
            )
            stored_raw = json.dumps(stored, ensure_ascii=False, separators=(",", ":"))
            self._set_unlocked(profile_key, stored_raw, None)
            if desired_username is not None:
                self._set_unlocked(f"username:{desired_username}", str(user_id), None)
            return stored_raw

    def sadd(self, key: str, *members: str) -> int:
        with self._lock:
            self._purge_if_expired(key)
            values = self._sets.setdefault(key, set())
            before = len(values)
            values.update(str(member) for member in members)
            return len(values) - before

    def sismember(self, key: str, member: str) -> bool:
        with self._lock:
            self._purge_if_expired(key)
            return str(member) in self._sets.get(key, set())

    def smembers(self, key: str) -> set[str]:
        with self._lock:
            self._purge_if_expired(key)
            return set(self._sets.get(key, set()))

    def set_add_expiring(self, key: str, members: set[str], ex: int) -> set[str]:
        if ex <= 0:
            raise ValueError("ex must be positive")
        with self._lock:
            self._purge_if_expired(key)
            values = self._sets.setdefault(key, set())
            values.update(str(member) for member in members)
            self._expiry[key] = time.time() + ex
            return set(values)

    def srem(self, key: str, *members: str) -> int:
        with self._lock:
            self._purge_if_expired(key)
            values = self._sets.get(key, set())
            removed = sum(1 for member in members if str(member) in values)
            values.difference_update(str(member) for member in members)
            if not values:
                self._sets.pop(key, None)
            return removed

    def delete_if_value(self, key: str, expected: str) -> bool:
        with self._lock:
            self._purge_if_expired(key)
            if self._values.get(key) != expected:
                return False
            self._values.pop(key, None)
            self._expiry.pop(key, None)
            return True

    def increment(self, key: str) -> int:
        with self._lock:
            self._purge_if_expired(key)
            try:
                value = int(self._values.get(key, "0")) + 1
            except ValueError:
                value = 1
            self._set_unlocked(key, str(value), None)
            return value

    def admin_role_change(self, user_id: int, *, add: bool) -> bool:
        member = str(user_id)
        version_key = f"adminver:{user_id}"
        with self._lock:
            members = self._sets.setdefault("admins", set())
            changed = member not in members if add else member in members
            if not changed:
                return False
            if add:
                members.add(member)
            else:
                members.remove(member)
                if not members:
                    self._sets.pop("admins", None)
            try:
                version = int(self._values.get(version_key, "0")) + 1
            except ValueError:
                version = 1
            self._set_unlocked(version_key, str(version), None)
            return True

    def rate_limit(self, key: str, limit: int, window_seconds: int) -> bool:
        if limit <= 0 or window_seconds <= 0:
            raise ValueError("rate limit values must be positive")
        with self._lock:
            self._purge_if_expired(key)
            try:
                count = int(self._values.get(key, "0")) + 1
            except ValueError:
                count = 1
            remaining = self._expiry.get(key)
            ttl = (
                max(int(remaining - time.time()), 1)
                if remaining is not None
                else window_seconds
            )
            self._set_unlocked(key, str(count), ttl)
            return count <= limit

    def indexed_json_put(
        self,
        index_key: str,
        item_key: str,
        member: str,
        value: str,
        *,
        create_only: bool,
    ) -> str:
        with self._lock:
            self._purge_if_expired(item_key)
            exists = item_key in self._values
            if create_only and exists:
                return "exists"
            self._set_unlocked(item_key, value, None)
            self._sets.setdefault(index_key, set()).add(member)
            return "created" if not exists else "updated"

    def indexed_delete(self, index_key: str, item_key: str, member: str) -> bool:
        with self._lock:
            existed = item_key in self._values
            self._values.pop(item_key, None)
            self._expiry.pop(item_key, None)
            values = self._sets.get(index_key)
            if values is not None:
                values.discard(member)
                if not values:
                    self._sets.pop(index_key, None)
            return existed

    def rename_indexed_set(
        self,
        index_key: str,
        old_item_key: str,
        new_item_key: str,
        old_members_key: str,
        new_members_key: str,
        old_member: str,
        new_member: str,
        value: str,
    ) -> str:
        with self._lock:
            if old_item_key not in self._values:
                return "missing"
            if old_item_key != new_item_key and new_item_key in self._values:
                return "exists"
            members = set(self._sets.get(old_members_key, set()))
            self._set_unlocked(new_item_key, value, None)
            self._sets.setdefault(index_key, set()).discard(old_member)
            self._sets[index_key].add(new_member)
            if new_members_key != old_members_key:
                if members:
                    self._sets[new_members_key] = members
                self._sets.pop(old_members_key, None)
            if old_item_key != new_item_key:
                self._values.pop(old_item_key, None)
                self._expiry.pop(old_item_key, None)
            return "renamed"

    def list_member_add(self, meta_key: str, members_key: str, member: str) -> str:
        with self._lock:
            if meta_key not in self._values:
                return "missing"
            values = self._sets.setdefault(members_key, set())
            if member in values:
                return "existing"
            values.add(member)
            return "added"

    def list_delete(
        self, index_key: str, meta_key: str, members_key: str, member: str
    ) -> bool:
        with self._lock:
            existed = meta_key in self._values
            self._values.pop(meta_key, None)
            self._expiry.pop(meta_key, None)
            self._sets.pop(members_key, None)
            index = self._sets.get(index_key)
            if index is not None:
                index.discard(member)
                if not index:
                    self._sets.pop(index_key, None)
            return existed

    def delete_user_alias(self, user_id: int) -> bool:
        profile_key = f"user:{user_id}"
        with self._lock:
            raw = self._values.get(profile_key)
            try:
                profile = json.loads(raw) if raw is not None else None
            except (TypeError, ValueError):
                profile = None
            username = profile.get("username") if isinstance(profile, dict) else None
            normalized_username = (
                username.strip().lstrip("@").casefold()
                if isinstance(username, str) and username.strip().lstrip("@")
                else None
            )
            existed = profile_key in self._values
            self._values.pop(profile_key, None)
            self._expiry.pop(profile_key, None)
            if normalized_username:
                alias_key = f"username:{normalized_username}"
                if self._values.get(alias_key) == str(user_id):
                    self._values.pop(alias_key, None)
                    self._expiry.pop(alias_key, None)
            return existed

    def delete_user_data(self, user_id: int) -> tuple[bool, int]:
        profile_key = f"user:{user_id}"
        with self._lock:
            raw = self._values.get(profile_key)
            try:
                profile = json.loads(raw) if raw is not None else None
            except (TypeError, ValueError):
                profile = None
            username = profile.get("username") if isinstance(profile, dict) else None
            normalized_username = (
                username.strip().lstrip("@").casefold()
                if isinstance(username, str) and username.strip().lstrip("@")
                else None
            )
            existed = profile_key in self._values
            self._values.pop(profile_key, None)
            self._expiry.pop(profile_key, None)
            if normalized_username:
                alias_key = f"username:{normalized_username}"
                if self._values.get(alias_key) == str(user_id):
                    self._values.pop(alias_key, None)
                    self._expiry.pop(alias_key, None)
            removed = 0
            for slug in self._sets.get("lists:index", set()):
                members_key = f"list:{slug}:members"
                members = self._sets.get(members_key)
                if members is not None and str(user_id) in members:
                    members.remove(str(user_id))
                    removed += 1
                    if not members:
                        self._sets.pop(members_key, None)
            return existed, removed

    def list_privacy_filter(
        self,
        key: str,
        user_id: int,
        outbound_message_ids: set[int],
    ) -> int:
        with self._lock:
            self._purge_if_expired(key)
            kept: list[str] = []
            for raw in self._lists.get(key, []):
                try:
                    record = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                if not isinstance(record, dict):
                    continue
                if record.get("user_id") == user_id:
                    continue
                if record.get("is_bot") is True and record.get("message_id") in outbound_message_ids:
                    continue
                reply = record.get("reply_to")
                if isinstance(reply, dict) and reply.get("user_id") == user_id:
                    reply = dict(reply)
                    reply["user_id"] = None
                    reply["text"] = None
                    record["reply_to"] = reply
                kept.append(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
            if kept:
                self._lists[key] = kept
            else:
                self._lists.pop(key, None)
                self._expiry.pop(key, None)
            return len(kept)

    def list_remove_message_ids(self, key: str, message_ids: set[int]) -> int:
        with self._lock:
            self._purge_if_expired(key)
            kept: list[str] = []
            for raw in self._lists.get(key, []):
                try:
                    record = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                if (
                    isinstance(record, dict)
                    and record.get("message_id") not in message_ids
                ):
                    kept.append(raw)
            if kept:
                self._lists[key] = kept
            else:
                self._lists.pop(key, None)
                self._expiry.pop(key, None)
            return len(kept)

    def delete(self, *keys: str) -> int:
        with self._lock:
            removed = 0
            for key in keys:
                existed = (
                    key in self._values
                    or key in self._lists
                    or key in self._sets
                    or key in self._expiry
                )
                self._values.pop(key, None)
                self._expiry.pop(key, None)
                self._lists.pop(key, None)
                self._sets.pop(key, None)
                if existed:
                    removed += 1
            return removed

    def backend(self) -> str:
        return "memory"


class UpstashKV:
    """Production store on top of the Upstash Redis REST SDK."""

    def __init__(self, url: str, token: str) -> None:
        self._r = build_upstash_redis(url, token)
        self._lock = threading.RLock()

    def _call(self, method: str, *args: object, **kwargs: object) -> object:
        lock = getattr(self, "_lock", None)
        if lock is None:
            lock = threading.RLock()
            self._lock = lock
        if not lock.acquire(timeout=UPSTASH_LOCK_TIMEOUT_SECONDS):
            raise TimeoutError("Redis client contention")
        try:
            return getattr(self._r, method)(*args, **kwargs)
        finally:
            lock.release()

    def ping(self) -> bool:
        return bool(self._call("ping"))

    def get(self, key: str) -> Optional[str]:
        value = self._call("get", key)
        return None if value is None else str(value)

    def set(self, key: str, value: str, ex: Optional[int] = None) -> None:
        self._call("set", key, value, ex=ex)

    def set_nx(self, key: str, value: str, ex: Optional[int] = None) -> bool:
        # Upstash .set(nx=True) returns "OK" on success and None if the key exists.
        return bool(self._call("set", key, value, nx=True, ex=ex))

    def lpush(self, key: str, value: str) -> int:
        return int(self._call("lpush", key, value))

    def ltrim(self, key: str, start: int, stop: int) -> None:
        self._call("ltrim", key, start, stop)

    def lrange(self, key: str, start: int, stop: int) -> list[str]:
        return list(self._call("lrange", key, start, stop))

    def llen(self, key: str) -> int:
        return int(self._call("llen", key))

    def list_upsert_json(
        self,
        key: str,
        identity_field: str,
        identity_value: str,
        value: str,
        limit: int,
        ex: Optional[int] = None,
        prune_field: Optional[str] = None,
        min_value: Optional[int] = None,
        block_key: Optional[str] = None,
    ) -> int:
        if limit <= 0:
            raise ValueError("limit must be positive")
        if ex is not None and ex <= 0:
            raise ValueError("ex must be positive")
        script = """
        if ARGV[8] == '1' and redis.call('EXISTS', KEYS[2]) == 1 then
            return redis.call('LLEN', KEYS[1])
        end

        local function integer_field(decoded, field)
            local value = decoded[field]
            if type(value) == 'number' and value % 1 == 0 then
                return value
            end
            return -1
        end

        local function version(decoded)
            local timestamp = integer_field(decoded, 'ts')
            local edit_timestamp = integer_field(decoded, 'edit_ts')
            local edited = 0
            if edit_timestamp >= 0 or decoded['is_edited'] == true then
                edited = 1
            end
            local effective = timestamp
            if edit_timestamp >= 0 then
                effective = edit_timestamp
            end
            return effective, edited, integer_field(decoded, 'source_update_id')
        end

        local function is_newer(left, right)
            local lt, le, lu = version(left)
            local rt, re, ru = version(right)
            if lt ~= rt then return lt > rt end
            if le ~= re then return le > re end
            return lu > ru
        end

        local items = redis.call('LRANGE', KEYS[1], 0, -1)
        local incoming_ok, incoming = pcall(cjson.decode, ARGV[3])
        if not incoming_ok or type(incoming) ~= 'table' then
            return redis.error_reply('history value must be a JSON object')
        end

        local selected = incoming
        local selected_raw = ARGV[3]
        local entries = {}
        for index, item in ipairs(items) do
            local ok, decoded = pcall(cjson.decode, item)
            if ok and type(decoded) == 'table'
                and tostring(decoded[ARGV[1]]) == ARGV[2] then
                if is_newer(decoded, selected) then
                    selected = decoded
                    selected_raw = item
                end
            else
                table.insert(entries, {
                    raw = item,
                    decoded = ok and type(decoded) == 'table' and decoded or nil,
                    original_index = index,
                })
            end
        end

        table.insert(entries, {
            raw = selected_raw,
            decoded = selected,
            original_index = #items + 1,
        })

        local kept = {}
        local minimum = tonumber(ARGV[7])
        for _, entry in ipairs(entries) do
            local keep = true
            if ARGV[6] ~= '' and minimum then
                keep = false
                if entry.decoded then
                    local raw_timestamp = entry.decoded[ARGV[6]]
                    if type(raw_timestamp) == 'number'
                        and raw_timestamp % 1 == 0
                        and raw_timestamp >= minimum then
                        keep = true
                    end
                end
            end
            if keep then
                table.insert(kept, entry)
            end
        end

        table.sort(kept, function(left, right)
            local left_valid = left.decoded ~= nil
            local right_valid = right.decoded ~= nil
            if left_valid ~= right_valid then return left_valid end
            if left_valid then
                local left_ts = integer_field(left.decoded, 'ts')
                local right_ts = integer_field(right.decoded, 'ts')
                if left_ts ~= right_ts then return left_ts > right_ts end
                local left_id = integer_field(left.decoded, 'message_id')
                local right_id = integer_field(right.decoded, 'message_id')
                if left_id ~= right_id then return left_id > right_id end
            end
            return left.original_index < right.original_index
        end)

        redis.call('DEL', KEYS[1])
        local limit = tonumber(ARGV[4])
        for index, entry in ipairs(kept) do
            if index > limit then break end
            redis.call('RPUSH', KEYS[1], entry.raw)
        end
        if tonumber(ARGV[5]) > 0 and redis.call('EXISTS', KEYS[1]) == 1 then
            redis.call('EXPIRE', KEYS[1], ARGV[5])
        end
        return redis.call('LLEN', KEYS[1])
        """
        return int(
            self._call(
                "eval",
                script,
                keys=[key, *([block_key] if block_key is not None else [])],
                args=[
                    identity_field,
                    identity_value,
                    value,
                    str(limit),
                    str(ex or 0),
                    prune_field or "",
                    str(min_value) if min_value is not None else "",
                    "1" if block_key is not None else "0",
                ],
            )
        )

    def list_prune_json(self, key: str, field: str, min_value: int) -> int:
        script = """
        local ttl = redis.call('TTL', KEYS[1])
        local current = redis.call('LRANGE', KEYS[1], 0, -1)
        local kept = {}
        for _, item in ipairs(current) do
            local ok, decoded = pcall(cjson.decode, item)
            if ok and type(decoded) == 'table' then
                local raw_value = decoded[ARGV[1]]
                local value = nil
                if type(raw_value) == 'number' and raw_value % 1 == 0 then
                    value = tonumber(raw_value)
                end
                if value and value >= tonumber(ARGV[2]) then
                    table.insert(kept, item)
                end
            end
        end
        redis.call('DEL', KEYS[1])
        for _, item in ipairs(kept) do
            redis.call('RPUSH', KEYS[1], item)
        end
        if #kept > 0 and ttl > 0 then
            redis.call('EXPIRE', KEYS[1], ttl)
        elseif ttl == 0 then
            redis.call('DEL', KEYS[1])
            return 0
        end
        return #kept
        """
        return int(self._call("eval", script, keys=[key], args=[field, str(min_value)]))

    def observe_user_json(
        self, user_id: int, normalized_username: Optional[str], value: str
    ) -> str:
        script = """
        local function decode_object(raw)
            if not raw then return nil end
            local ok, decoded = pcall(cjson.decode, raw)
            if ok and type(decoded) == 'table' then return decoded end
            return nil
        end

        local function integer_field(record, field)
            local value = record[field]
            if type(value) == 'number' and value % 1 == 0 then return value end
            return -1
        end

        local function compare_order(left, right)
            local left_seen = integer_field(left, 'last_seen_at')
            local right_seen = integer_field(right, 'last_seen_at')
            if left_seen ~= right_seen then
                return left_seen > right_seen and 1 or -1
            end
            local left_update = integer_field(left, 'last_update_id')
            local right_update = integer_field(right, 'last_update_id')
            if left_update ~= right_update then
                return left_update > right_update and 1 or -1
            end
            return 0
        end

        local function normalize_username(value)
            if type(value) ~= 'string' then return nil end
            value = string.gsub(value, '^%s+', '')
            value = string.gsub(value, '%s+$', '')
            value = string.gsub(value, '^@+', '')
            value = string.lower(value)
            if value == '' then return nil end
            return value
        end

        local incoming_ok, incoming = pcall(cjson.decode, ARGV[2])
        if not incoming_ok or type(incoming) ~= 'table' then
            return redis.error_reply('user value must be a JSON object')
        end

        local user_id = ARGV[1]
        local current_raw = redis.call('GET', KEYS[1])
        local current = decode_object(current_raw)
        if current and compare_order(current, incoming) > 0 then
            return current_raw
        end

        local desired = ARGV[3]
        if desired == '' then desired = nil end
        if desired then
            local alias_key = 'username:' .. desired
            local owner_id = redis.call('GET', alias_key)
            if owner_id and owner_id ~= user_id then
                local owner_key = 'user:' .. owner_id
                local owner_raw = redis.call('GET', owner_key)
                local owner = decode_object(owner_raw)
                if owner and compare_order(owner, incoming) >= 0 then
                    desired = nil
                else
                    if owner and normalize_username(owner['username']) == ARGV[3] then
                        owner['username'] = cjson.null
                        redis.call('SET', owner_key, cjson.encode(owner))
                    end
                    redis.call('SET', alias_key, user_id)
                end
            end
        end

        if current then
            local old_username = normalize_username(current['username'])
            if old_username and old_username ~= desired then
                local old_alias_key = 'username:' .. old_username
                if redis.call('GET', old_alias_key) == user_id then
                    redis.call('DEL', old_alias_key)
                end
            end
        end

        if desired then
            redis.call('SET', 'username:' .. desired, user_id)
        else
            incoming['username'] = cjson.null
        end
        local stored = cjson.encode(incoming)
        redis.call('SET', KEYS[1], stored)
        return stored
        """
        return str(
            self._call(
                "eval",
                script,
                keys=[f"user:{user_id}"],
                args=[str(user_id), value, normalized_username or ""],
            )
        )

    def sadd(self, key: str, *members: str) -> int:
        if not members:
            return 0
        return int(self._call("sadd", key, *members))

    def sismember(self, key: str, member: str) -> bool:
        return bool(self._call("sismember", key, member))

    def smembers(self, key: str) -> set[str]:
        return {str(item) for item in (self._call("smembers", key) or [])}

    def set_add_expiring(self, key: str, members: set[str], ex: int) -> set[str]:
        if ex <= 0:
            raise ValueError("ex must be positive")
        script = """
        for index = 2, #ARGV do redis.call('SADD', KEYS[1], ARGV[index]) end
        redis.call('EXPIRE', KEYS[1], ARGV[1])
        return redis.call('SMEMBERS', KEYS[1])
        """
        result = self._call(
            "eval",
            script,
            keys=[key],
            args=[str(ex), *sorted(members)],
        )
        return {str(item) for item in (result or [])}

    def srem(self, key: str, *members: str) -> int:
        return int(self._call("srem", key, *members))

    def delete_if_value(self, key: str, expected: str) -> bool:
        script = "if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('DEL', KEYS[1]) else return 0 end"
        return bool(self._call("eval", script, keys=[key], args=[expected]))

    def increment(self, key: str) -> int:
        return int(self._call("incr", key))

    def admin_role_change(self, user_id: int, *, add: bool) -> bool:
        script = """
        local member = ARGV[1]
        local changed
        if ARGV[2] == '1' then
            changed = redis.call('SADD', KEYS[1], member)
        else
            changed = redis.call('SREM', KEYS[1], member)
        end
        if changed == 1 then redis.call('INCR', KEYS[2]) end
        return changed
        """
        return bool(
            self._call(
                "eval",
                script,
                keys=["admins", f"adminver:{user_id}"],
                args=[str(user_id), "1" if add else "0"],
            )
        )

    def rate_limit(self, key: str, limit: int, window_seconds: int) -> bool:
        if limit <= 0 or window_seconds <= 0:
            raise ValueError("rate limit values must be positive")
        script = """
        local count = redis.call('INCR', KEYS[1])
        if count == 1 then redis.call('EXPIRE', KEYS[1], ARGV[2]) end
        return count <= tonumber(ARGV[1]) and 1 or 0
        """
        return bool(
            self._call(
                "eval",
                script,
                keys=[key],
                args=[str(limit), str(window_seconds)],
            )
        )

    def indexed_json_put(
        self,
        index_key: str,
        item_key: str,
        member: str,
        value: str,
        *,
        create_only: bool,
    ) -> str:
        script = """
        local exists = redis.call('EXISTS', KEYS[2]) == 1
        if ARGV[3] == '1' and exists then return 'exists' end
        redis.call('SET', KEYS[2], ARGV[2])
        redis.call('SADD', KEYS[1], ARGV[1])
        return exists and 'updated' or 'created'
        """
        return str(
            self._call(
                "eval",
                script,
                keys=[index_key, item_key],
                args=[member, value, "1" if create_only else "0"],
            )
        )

    def indexed_delete(self, index_key: str, item_key: str, member: str) -> bool:
        script = "local removed = redis.call('DEL', KEYS[2]); redis.call('SREM', KEYS[1], ARGV[1]); return removed"
        return bool(
            self._call(
                "eval", script, keys=[index_key, item_key], args=[member]
            )
        )

    def rename_indexed_set(
        self,
        index_key: str,
        old_item_key: str,
        new_item_key: str,
        old_members_key: str,
        new_members_key: str,
        old_member: str,
        new_member: str,
        value: str,
    ) -> str:
        script = """
        if redis.call('EXISTS', KEYS[2]) == 0 then return 'missing' end
        if KEYS[2] ~= KEYS[3] and redis.call('EXISTS', KEYS[3]) == 1 then
            return 'exists'
        end
        redis.call('SET', KEYS[3], ARGV[3])
        if KEYS[4] ~= KEYS[5] then
            local members = redis.call('SMEMBERS', KEYS[4])
            if #members > 0 then redis.call('SADD', KEYS[5], unpack(members)) end
            redis.call('DEL', KEYS[4])
        end
        redis.call('SREM', KEYS[1], ARGV[1])
        redis.call('SADD', KEYS[1], ARGV[2])
        if KEYS[2] ~= KEYS[3] then redis.call('DEL', KEYS[2]) end
        return 'renamed'
        """
        return str(
            self._call(
                "eval",
                script,
                keys=[
                    index_key,
                    old_item_key,
                    new_item_key,
                    old_members_key,
                    new_members_key,
                ],
                args=[old_member, new_member, value],
            )
        )

    def list_member_add(self, meta_key: str, members_key: str, member: str) -> str:
        script = """
        if redis.call('EXISTS', KEYS[1]) == 0 then return 'missing' end
        local changed = redis.call('SADD', KEYS[2], ARGV[1])
        return changed == 1 and 'added' or 'existing'
        """
        return str(
            self._call(
                "eval",
                script,
                keys=[meta_key, members_key],
                args=[member],
            )
        )

    def list_delete(
        self, index_key: str, meta_key: str, members_key: str, member: str
    ) -> bool:
        script = """
        local removed = redis.call('DEL', KEYS[2])
        redis.call('DEL', KEYS[3])
        redis.call('SREM', KEYS[1], ARGV[1])
        return removed
        """
        return bool(
            self._call(
                "eval",
                script,
                keys=[index_key, meta_key, members_key],
                args=[member],
            )
        )

    def delete_user_alias(self, user_id: int) -> bool:
        script = """
        local raw = redis.call('GET', KEYS[1])
        if not raw then return 0 end
        local ok, profile = pcall(cjson.decode, raw)
        if ok and type(profile) == 'table' and type(profile['username']) == 'string' then
            local username = profile['username']
            username = string.gsub(username, '^%s+', '')
            username = string.gsub(username, '%s+$', '')
            username = string.gsub(username, '^@+', '')
            username = string.lower(username)
            if username ~= '' then
                local alias_key = 'username:' .. username
                if redis.call('GET', alias_key) == ARGV[1] then
                    redis.call('DEL', alias_key)
                end
            end
        end
        return redis.call('DEL', KEYS[1])
        """
        profile_key = f"user:{user_id}"
        return bool(
            self._call(
                "eval",
                script,
                keys=[profile_key],
                args=[str(user_id)],
            )
        )

    def delete_user_data(self, user_id: int) -> tuple[bool, int]:
        script = """
        local raw = redis.call('GET', KEYS[1])
        local existed = raw and 1 or 0
        if raw then
            local ok, profile = pcall(cjson.decode, raw)
            if ok and type(profile) == 'table' and type(profile['username']) == 'string' then
                local username = profile['username']
                username = string.gsub(username, '^%s+', '')
                username = string.gsub(username, '%s+$', '')
                username = string.gsub(username, '^@+', '')
                username = string.lower(username)
                if username ~= '' then
                    local alias_key = 'username:' .. username
                    if redis.call('GET', alias_key) == ARGV[1] then
                        redis.call('DEL', alias_key)
                    end
                end
            end
        end
        redis.call('DEL', KEYS[1])
        local removed = 0
        for _, slug in ipairs(redis.call('SMEMBERS', KEYS[2])) do
            removed = removed + redis.call('SREM', 'list:' .. slug .. ':members', ARGV[1])
        end
        return {existed, removed}
        """
        result = self._call(
            "eval",
            script,
            keys=[f"user:{user_id}", "lists:index"],
            args=[str(user_id)],
        )
        if not isinstance(result, (list, tuple)) or len(result) != 2:
            raise RuntimeError("invalid Redis user deletion response")
        return bool(int(result[0])), int(result[1])

    def list_privacy_filter(
        self,
        key: str,
        user_id: int,
        outbound_message_ids: set[int],
    ) -> int:
        script = """
        local ttl = redis.call('PTTL', KEYS[1])
        if ttl <= 0 then redis.call('DEL', KEYS[1]); return 0 end
        local outbound = cjson.decode(ARGV[2])
        local current = redis.call('LRANGE', KEYS[1], 0, -1)
        local kept = {}
        for _, raw in ipairs(current) do
            local ok, record = pcall(cjson.decode, raw)
            if ok and type(record) == 'table' and record['user_id'] ~= tonumber(ARGV[1]) then
                local message_id = tostring(record['message_id'] or '')
                local remove_bot = record['is_bot'] == true and outbound[message_id] == true
                if not remove_bot then
                    local reply = record['reply_to']
                    if type(reply) == 'table' and reply['user_id'] == tonumber(ARGV[1]) then
                        reply['user_id'] = cjson.null
                        reply['text'] = cjson.null
                    end
                    table.insert(kept, cjson.encode(record))
                end
            end
        end
        redis.call('DEL', KEYS[1])
        for _, raw in ipairs(kept) do redis.call('RPUSH', KEYS[1], raw) end
        if #kept > 0 then redis.call('PEXPIRE', KEYS[1], ttl) end
        return #kept
        """
        outbound = {str(message_id): True for message_id in outbound_message_ids}
        return int(
            self._call(
                "eval",
                script,
                keys=[key],
                args=[str(user_id), json.dumps(outbound, separators=(",", ":"))],
            )
        )

    def list_remove_message_ids(self, key: str, message_ids: set[int]) -> int:
        script = """
        local ttl = redis.call('PTTL', KEYS[1])
        if ttl <= 0 then redis.call('DEL', KEYS[1]); return 0 end
        local removed = cjson.decode(ARGV[1])
        local current = redis.call('LRANGE', KEYS[1], 0, -1)
        local kept = {}
        for _, raw in ipairs(current) do
            local ok, record = pcall(cjson.decode, raw)
            local message_id = ok and type(record) == 'table' and tostring(record['message_id'] or '') or ''
            if ok and type(record) == 'table' and removed[message_id] ~= true then
                table.insert(kept, raw)
            end
        end
        redis.call('DEL', KEYS[1])
        for _, raw in ipairs(kept) do redis.call('RPUSH', KEYS[1], raw) end
        if #kept > 0 then redis.call('PEXPIRE', KEYS[1], ttl) end
        return #kept
        """
        removed = {str(message_id): True for message_id in message_ids}
        return int(
            self._call(
                "eval",
                script,
                keys=[key],
                args=[json.dumps(removed, separators=(",", ":"))],
            )
        )

    def delete(self, *keys: str) -> int:
        if not keys:
            return 0
        return int(self._call("delete", *keys))

    def backend(self) -> str:
        return "upstash"


_store: Optional[KV] = None
_store_lock = threading.Lock()


def _build_store() -> KV:
    if settings.UPSTASH_REDIS_REST_URL and settings.UPSTASH_REDIS_REST_TOKEN:
        return UpstashKV(
            settings.UPSTASH_REDIS_REST_URL, settings.UPSTASH_REDIS_REST_TOKEN
        )
    return MemoryKV()


def get_store() -> KV:
    """Lazy store singleton (reduces cold start: the client is built on first use)."""
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                _store = _build_store()
    return _store


def reset_store() -> None:
    """Reset the singleton (used in tests)."""
    global _store
    with _store_lock:
        _store = None
