from typing import Any, Optional

from .database import Database


CONTACT_COLUMNS = """
id,
first_name,
last_name,
email_address,
company,
title,
notes,
source,
created_at,
updated_at
"""

CONTACT_FIELDS = ("first_name", "last_name", "email_address", "company", "title", "notes")
CONTACT_LIMITS = {
    "first_name": 120,
    "last_name": 120,
    "email_address": 320,
    "company": 240,
    "title": 240,
    "notes": 10000,
}
CONTACT_SOURCES = {"dashboard", "agent"}


class ContactStore:
    def __init__(self, db: Database):
        self.db = db

    def clean_field(self, name: str, value: Any) -> str:
        text = str(value or "").replace("\x00", "[NUL]").strip()
        if name == "email_address":
            text = text.lower()
        return text[: CONTACT_LIMITS[name]]

    def clean_source(self, source: str) -> str:
        clean = str(source or "").strip().lower()
        if clean not in CONTACT_SOURCES:
            raise ValueError("contact source must be dashboard or agent")
        return clean

    def clean_fields(self, fields: dict[str, Any]) -> dict[str, str]:
        cleaned = {}
        for name, value in fields.items():
            if name not in CONTACT_FIELDS:
                continue
            cleaned[name] = self.clean_field(name, value)
        self.validate_email(cleaned.get("email_address"))
        return cleaned

    def validate_email(self, email_address: Optional[str]) -> None:
        if not email_address:
            return
        if "@" not in email_address or any(char.isspace() for char in email_address):
            raise ValueError("email address is invalid")

    def validate_not_blank(self, fields: dict[str, Any]) -> None:
        if not any(str(fields.get(name) or "").strip() for name in CONTACT_FIELDS):
            raise ValueError("at least one contact field must be non-empty")

    def get(self, contact_id: int) -> Optional[dict[str, Any]]:
        return self.db.fetch_one(
            f"SELECT {CONTACT_COLUMNS} FROM contacts WHERE id = %s",
            (contact_id,),
        )

    def get_by_email(self, email_address: str) -> Optional[dict[str, Any]]:
        clean_email = self.clean_field("email_address", email_address)
        if not clean_email:
            return None
        return self.db.fetch_one(
            f"SELECT {CONTACT_COLUMNS} FROM contacts WHERE lower(email_address) = lower(%s)",
            (clean_email,),
        )

    def search(self, query: str = "", limit: int = 100) -> list[dict[str, Any]]:
        clean_query = str(query or "").strip()
        try:
            requested_limit = int(limit or 100)
        except (TypeError, ValueError):
            requested_limit = 100
        max_rows = min(max(requested_limit, 1), 500)
        if not clean_query:
            return self.db.fetch_all(
                f"""
                SELECT {CONTACT_COLUMNS}
                FROM contacts
                ORDER BY updated_at DESC, created_at DESC, id DESC
                LIMIT %s
                """,
                (max_rows,),
            )
        pattern = "%%%s%%" % clean_query
        return self.db.fetch_all(
            f"""
            SELECT {CONTACT_COLUMNS}
            FROM contacts
            WHERE first_name ILIKE %s
               OR last_name ILIKE %s
               OR email_address ILIKE %s
               OR company ILIKE %s
               OR title ILIKE %s
               OR notes ILIKE %s
            ORDER BY updated_at DESC, created_at DESC, id DESC
            LIMIT %s
            """,
            (pattern, pattern, pattern, pattern, pattern, pattern, max_rows),
        )

    def create(self, fields: dict[str, Any], source: str = "agent") -> dict[str, Any]:
        cleaned = {name: "" for name in CONTACT_FIELDS}
        cleaned.update(self.clean_fields(fields))
        self.validate_not_blank(cleaned)
        clean_source = self.clean_source(source)
        if cleaned["email_address"] and self.get_by_email(cleaned["email_address"]):
            raise ValueError("contact with email address already exists")
        row = self.db.fetch_one(
            f"""
            INSERT INTO contacts(first_name, last_name, email_address, company, title, notes, source)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING {CONTACT_COLUMNS}
            """,
            (
                cleaned["first_name"],
                cleaned["last_name"],
                cleaned["email_address"],
                cleaned["company"],
                cleaned["title"],
                cleaned["notes"],
                clean_source,
            ),
        )
        if row is None:
            raise ValueError("contact was not created")
        return row

    def update(self, contact_id: int, fields: dict[str, Any]) -> dict[str, Any]:
        existing = self.get(contact_id)
        if existing is None:
            raise ValueError("contact not found")
        cleaned = self.clean_fields(fields)
        if not cleaned:
            raise ValueError("at least one contact field must be provided")
        next_values = dict(existing)
        next_values.update(cleaned)
        self.validate_not_blank(next_values)
        if "email_address" in cleaned and cleaned["email_address"]:
            duplicate = self.get_by_email(cleaned["email_address"])
            if duplicate and int(duplicate["id"]) != int(contact_id):
                raise ValueError("contact with email address already exists")

        assignments = []
        params: list[Any] = []
        for name in CONTACT_FIELDS:
            if name not in cleaned:
                continue
            assignments.append("%s = %%s" % name)
            params.append(cleaned[name])
        params.append(contact_id)
        row = self.db.fetch_one(
            f"""
            UPDATE contacts
            SET {", ".join(assignments)},
                updated_at = now()
            WHERE id = %s
            RETURNING {CONTACT_COLUMNS}
            """,
            tuple(params),
        )
        if row is None:
            raise ValueError("contact not found")
        return row

    def delete(self, contact_id: int) -> dict[str, Any]:
        row = self.db.fetch_one(
            f"DELETE FROM contacts WHERE id = %s RETURNING {CONTACT_COLUMNS}",
            (contact_id,),
        )
        if row is None:
            raise ValueError("contact not found")
        return row
