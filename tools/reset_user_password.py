"""Admin utility: reset a user's password directly in the database.

No SMTP/email flow is wired up (the /auth/forgot-password endpoint is a stub).
When a user asks for a reset, run this to set a new password — it is bcrypt-hashed
with the exact same function the app uses at registration, so the user can log in
immediately. Works for password accounts and for Google-SSO-only accounts (it just
sets a password on them).

The reset also bumps token_version, which revokes every JWT minted before the
reset (same revocation the /auth/logout-all route uses). This matters when a
reset is the response to a compromise: without it, a stolen token would keep
passing the per-request token_version re-check until it expired (up to
JWT_EXPIRY_HOURS), so resetting the password would not actually lock the
attacker out.

Run:  venv/Scripts/python.exe -m tools.reset_user_password <email>
(The new password is prompted — never pass it on the command line, where it
would land in shell history and process listings.)
"""

import getpass
import sys

from src.backend.auth.repository import MAX_PASSWORD_BYTES, UserRepository

MIN_LEN = 8


def main() -> None:
    if len(sys.argv) != 2:
        print("usage: python -m tools.reset_user_password <email>")
        sys.exit(2)
    email = sys.argv[1].strip().lower()
    new_password = getpass.getpass("New password: ")
    if len(new_password) < MIN_LEN:
        print(f"refusing: password must be at least {MIN_LEN} characters")
        sys.exit(2)
    if len(new_password.encode("utf-8")) > MAX_PASSWORD_BYTES:
        print(f"refusing: password must be at most {MAX_PASSWORD_BYTES} bytes (bcrypt limit)")
        sys.exit(2)
    if getpass.getpass("Confirm new password: ") != new_password:
        print("refusing: passwords do not match")
        sys.exit(2)

    # Go through the repository so the password hashing, the token_version
    # revocation, and the users-table SQL stay in the one place the app owns
    # them (repository.py). The token_version bump revokes any token minted
    # before this reset.
    row = UserRepository().reset_password(email, new_password)

    if row is None:
        print(f"no user found with email {email!r}")
        sys.exit(1)
    print(f"password reset for {email} "
          f"(id={row['id']}, role={row['role']}, provider={row['auth_provider']}); "
          "all existing sessions revoked")


if __name__ == "__main__":
    main()
