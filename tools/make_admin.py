"""Admin utility: make a platform admin from the host.

A signup never grants admin — an email address is not proof that the caller owns
it. The first platform admin is made here instead, by whoever has a shell on the
host; after that, admins grant admin in the Access Console.

- No account for the address: asks a password and creates the account.
- A password account (possibly registered by someone else first): asks a NEW
  password and resets it, which revokes every session of that account.
- A Google account (google_sub set): no password asked — Google verified the
  address when the account was created.
Then the account is made admin and active, and one audit_log row records it.

Run:  docker exec -it nlrf-fastapi python -m tools.make_admin <email>
      (from a checkout: venv/Scripts/python.exe -m tools.make_admin <email>)

Referenced by: README.md (Quick Start step 5), docs/CONFIGURATION.md, main.py
(the ADMIN_EMAILS startup warning).
Depends on: auth/repository.py, auth/endpoints.py (the signup email rule),
core/audit_log.py, tools/reset_user_password.py (the password prompt).
"""

import json
import sys

from src.backend.auth.endpoints import _normalize_email
from src.backend.auth.repository import EmailAlreadyExists, UserRepository
from src.backend.core.audit_log import write_audit_log
from tools.reset_user_password import prompt_new_password


def _exit_if_gone(email: str, gone: bool) -> None:
    """A repository call returns None when its row was deleted mid-run; never report a grant then."""
    if gone:
        print(f"{email} disappeared while this ran; nothing was granted. Run the command again.")
        sys.exit(1)


def main() -> None:
    if len(sys.argv) != 2:
        print("usage: python -m tools.make_admin <email>")
        sys.exit(2)
    try:
        email = _normalize_email(sys.argv[1])
    except ValueError:
        print(f"refusing: {sys.argv[1]!r} is not a valid email")
        sys.exit(2)

    repo = UserRepository()
    row = repo.get_by_email(email)
    existed = row is not None
    if row is None:
        try:
            row = repo.create_user(email, prompt_new_password())
        except EmailAlreadyExists:
            print(f"{email} was registered while this ran; run the command again")
            sys.exit(1)
        password = "created"
    elif row.get("google_sub"):
        password = "unchanged (Google account)"
    else:
        # Whoever registered this address first loses it: the reset revokes every token.
        gone = repo.reset_password(email, prompt_new_password()) is None
        password = "reset, every session revoked"
        _exit_if_gone(email, gone)

    user_id = str(row["id"])
    _exit_if_gone(email, repo.set_platform_role(user_id, "admin") is None)
    # bump_token revokes tokens minted while the account was pending (org_id None, role user).
    _exit_if_gone(email, repo.set_status(user_id, "active", bump_token=True) is None)
    # A row the command just created had no earlier role or status.
    was_role, was_status = (row["role"], row["status"]) if existed else (None, None)
    detail = {"email": email, "from_role": was_role, "from_status": was_status,
              "to_role": "admin", "to_status": "active", "password": password}
    try:
        write_audit_log(actor_email="host-operator", actor_user_id=None, org_id=None,
                        method="CLI", path="tools.make_admin", status_code=200,
                        request_id=None, source="cli", detail=json.dumps(detail))
    except Exception as exc:  # noqa: BLE001 — the grant is done; the record is best-effort
        print(f"warning: admin granted but the audit row was not written: {exc}")
    was = f"was {row['role']}/{row['status']}" if existed else "was: new account"
    print(f"{email} is now an active platform admin (id={user_id}; {was}; "
          f"password {password}). Sign in at the web app.")


if __name__ == "__main__":
    main()
