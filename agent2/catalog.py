"""Read-only school / program catalog from Agent1 RDS."""

from agent1 import storage


def list_schools(*, country: str | None = None):
    sql = "SELECT school_id, name, country, city FROM school"
    params: tuple = ()
    if country:
        sql += " WHERE country=%s"
        params = (country,)
    sql += " ORDER BY name"
    with storage.read_session() as db:
        rows = db.execute(sql, params).fetchall()
    return [
        {"school_id": r["school_id"], "name": r["name"],
         "country": r["country"], "city": r["city"]}
        for r in rows
    ]


def list_programs(school_id: str):
    with storage.read_session() as db:
        rows = db.execute(
            """SELECT program_id, school_id, name, level, scope
               FROM program
               WHERE school_id=%s
                 AND (scope IS NULL OR scope = 'programme')
               ORDER BY name, level NULLS LAST""",
            (school_id,),
        ).fetchall()
    # Deduplicate identical name+level pairs (keep first program_id).
    seen, out = set(), []
    for r in rows:
        key = (r["name"], r["level"])
        if key in seen:
            continue
        seen.add(key)
        label = r["name"] if not r["level"] else f"{r['name']}（{r['level']}）"
        out.append({
            "program_id": r["program_id"],
            "school_id": r["school_id"],
            "name": r["name"],
            "level": r["level"],
            "label": label,
        })
    return out


def school_name(school_id: str) -> str | None:
    with storage.read_session() as db:
        row = db.execute(
            "SELECT name FROM school WHERE school_id=%s", (school_id,)
        ).fetchone()
    return row["name"] if row else None


def program_label(program_id: str) -> str | None:
    with storage.read_session() as db:
        row = db.execute(
            "SELECT name, level FROM program WHERE program_id=%s", (program_id,)
        ).fetchone()
    if not row:
        return None
    return row["name"] if not row["level"] else f"{row['name']}（{row['level']}）"
