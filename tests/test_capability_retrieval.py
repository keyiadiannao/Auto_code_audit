"""Tests for the PR-2 multi-channel capability retrieval."""
from __future__ import annotations

import capability_retrieval as cr


def _write(root, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_structural_retrieval_surfaces_renamed_duplicate(tmp_path) -> None:
    _write(
        tmp_path,
        "lib/session.py",
        'def refresh_session(session_id, token_store):\n'
        '    """Renew a session: mint a fresh token and store it with a TTL."""\n'
        "    token = token_store.issue(session_id)\n"
        "    token_store.put(session_id, token, ttl=3600)\n"
        "    return token\n",
    )
    _write(
        tmp_path,
        "experiments/new.py",
        'def rotate_credentials(cred_id, store):\n'
        '    """Rotate a credential: generate a new secret and write it back with expiry."""\n'
        "    secret = store.mint(cred_id)\n"
        "    store.write(cred_id, secret, expiry=3600)\n"
        "    return secret\n",
    )

    index = cr.build_index(tmp_path / "lib", rel_root=tmp_path)
    new_syms = {
        s.key: s for s in cr.build_index(tmp_path / "experiments", rel_root=tmp_path)
    }
    query = new_syms["experiments/new.py:rotate_credentials"]

    ranked = cr.retrieve(query, index, k=10)
    assert ranked, "retrieval returned no candidates"
    assert ranked[0][1].key == "lib/session.py:refresh_session"


def test_string_literal_channel_surfaces_repository_bypass(tmp_path) -> None:
    _write(
        tmp_path,
        "lib/repo.py",
        "class UserRepository:\n"
        "    def find_active(self, user_id):\n"
        '        """Return the active user row."""\n'
        '        rows = self.db.query("SELECT * FROM users WHERE id=? AND active=1", user_id)\n'
        "        return rows[0] if rows else None\n",
    )
    _write(
        tmp_path,
        "experiments/new.py",
        "def load_active_user(uid):\n"
        '    """Fetch the active user record directly."""\n'
        '    row = db.execute("SELECT * FROM users WHERE id=? AND active=1", uid).fetchone()\n'
        "    return row\n",
    )

    index = cr.build_index(tmp_path / "lib", rel_root=tmp_path)
    new_syms = {
        s.key: s for s in cr.build_index(tmp_path / "experiments", rel_root=tmp_path)
    }
    query = new_syms["experiments/new.py:load_active_user"]

    ranked = cr.retrieve(query, index, k=10)
    keys = [sym.key for _, sym in ranked]
    assert "lib/repo.py:UserRepository.find_active" in keys


def test_one_hop_closure_recovers_composite(tmp_path) -> None:
    _write(
        tmp_path,
        "lib/storage.py",
        "class StorageService:\n"
        "    def upload(self, payload, namespace, metadata=None):\n"
        '        """Write a blob to object storage."""\n'
        "        key = self._make_key(namespace, payload.name)\n"
        "        self.client.put_object(key, payload.bytes, metadata=metadata)\n"
        '        return {"key": key, "namespace": namespace}\n',
    )
    _write(
        tmp_path,
        "experiments/media.py",
        "def upload_avatar(file, scope, meta=None):\n"
        '    """Push an avatar image into storage."""\n'
        "    key = build_key(scope, file.filename)\n"
        "    object_store.put(key, file.content, metadata=meta)\n"
        '    return {"locator": key, "scope": scope}\n'
        "\n"
        "def save_avatar_variant(img, scope, dims):\n"
        '    """Validate, then store a resized variant."""\n'
        "    if not valid_image(img):\n"
        '        raise ValueError("invalid avatar image")\n'
        '    return upload_avatar(normalize(img, dims), scope, meta={"variant": dims})\n',
    )

    index = cr.build_index(tmp_path / "lib", rel_root=tmp_path)
    new_syms = {
        s.key: s for s in cr.build_index(tmp_path / "experiments", rel_root=tmp_path)
    }
    results = cr.retrieve_with_closure(new_syms, index, k=10)
    keys = [
        sym.key for _, sym in results["experiments/media.py:save_avatar_variant"]
    ]
    # The composite calls upload_avatar (its own duplicate), so it must
    # transitively surface the canonical StorageService.upload.
    assert "lib/storage.py:StorageService.upload" in keys
