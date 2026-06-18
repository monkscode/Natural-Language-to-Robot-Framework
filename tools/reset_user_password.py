"""Admin utility: reset a user's password directly in the database.

No SMTP/email flow is wired up (the /auth/forgot-password endpoint is a stub).
When a user asks for a reset, run this to set a new password — it is bcrypt-hashed
with the exact same function the app uses at registration, so the user can log in
immediately. Works for password accounts and for Google-SSO-only accounts (it just
sets a password on them).

Run:  venv/Scripts/python.exe -m tools.reset_user_password <email>
(The new password is prompted — never pass it on the command line, where it
would land in shell history and process listings.)
"""

import getpass
import sys

import psycopg

from src.backend.core.config import settings
from src.backend.auth.repository import MAX_PASSWORD_BYTES, hash_password

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

    hashed = hash_password(new_password)
    with psycopg.connect(settings.DATABASE_URL) as conn:
        row = conn.execute(
            "UPDATE users SET hashed_password = %s WHERE email = %s "
            "RETURNING id, email, role, auth_provider",
            (hashed, email),
        ).fetchone()
        conn.commit()

    if row is None:
        print(f"no user found with email {email!r}")
        sys.exit(1)
    print(f"password reset for {email} (id={row[0]}, role={row[2]}, provider={row[3]})")


if __name__ == "__main__":
    main()
