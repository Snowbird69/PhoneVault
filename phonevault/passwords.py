"""Backup passwords live in the system password vault (KDE Wallet / GNOME Keyring via the
Secret Service, or Windows Credential Locker) - never in PhoneVault's own files."""
SERVICE = "PhoneVault iPhone backup"


def vault_status() -> dict:
    """Is a usable password vault available? Does a write + read + delete round trip."""
    try:
        import keyring
        from keyring.backends import fail
    except ImportError:
        return {"ok": False, "backend": None, "error": "The keyring add-on isn't installed (pip install keyring)."}
    try:
        kr = keyring.get_keyring()
        name = f"{type(kr).__module__}.{type(kr).__name__}"
        if isinstance(kr, fail.Keyring) or "null" in name.lower() or "fail" in name.lower():
            return {"ok": False, "backend": name, "error": "No system password vault found. Is KDE Wallet enabled?"}
        keyring.set_password(SERVICE, "_phonevault_test", "ok")
        good = keyring.get_password(SERVICE, "_phonevault_test") == "ok"
        keyring.delete_password(SERVICE, "_phonevault_test")
        return {"ok": good, "backend": name, "error": None if good else "The vault didn't return what was stored."}
    except Exception as e:
        return {"ok": False, "backend": None, "error": f"Password vault error: {e}"}


def get(udid: str) -> str | None:
    try:
        import keyring
        return keyring.get_password(SERVICE, udid.upper())
    except Exception:
        return None


def put(udid: str, password: str):
    import keyring
    keyring.set_password(SERVICE, udid.upper(), password)
    if keyring.get_password(SERVICE, udid.upper()) != password:
        raise RuntimeError("The password vault didn't keep the password.")


def remove(udid: str):
    try:
        import keyring
        keyring.delete_password(SERVICE, udid.upper())
    except Exception:
        pass
