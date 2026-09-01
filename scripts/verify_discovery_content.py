"""Read-only integrity report for the installed public discovery corpus."""

from sqlalchemy import text

from app.database import engine


QUERIES = {
    "counts": """
        SELECT
          (SELECT count(*) FROM public_material_packs) AS packs,
          (SELECT count(*) FROM public_material_items) AS items,
          (SELECT count(*) FROM user_material_states) AS states
    """,
    "pack_counts": """
        SELECT p.code, p.kind, count(i.id) AS items
        FROM public_material_packs p
        LEFT JOIN public_material_items i ON i.pack_id = p.id
        GROUP BY p.id, p.code, p.kind, p.sort_order
        ORDER BY p.sort_order
    """,
    "daily_quote_count": """
        SELECT count(*)
        FROM public_material_items i
        JOIN public_material_packs p ON p.id = i.pack_id
        WHERE p.code = 'daily-quote'
    """,
    "corpus_signature": """
        SELECT md5(string_agg(
          p.code || '|' || i.id::text || '|' || i.position::text || '|' ||
          i.content_normalized || '|' || i.status,
          E'\n' ORDER BY p.code, i.position, i.id
        ))
        FROM public_material_items i
        JOIN public_material_packs p ON p.id = i.pack_id
    """,
    "samples": """
        SELECT p.code, i.content, i.chinese, i.card_type, i.source_label
        FROM public_material_items i
        JOIN public_material_packs p ON p.id = i.pack_id
        WHERE i.status = 'approved'
        ORDER BY random()
        LIMIT 10
    """,
    "integrity": """
        SELECT
          (SELECT count(*) FROM (
             SELECT id FROM public_material_items GROUP BY id HAVING count(*) > 1
           ) d) AS duplicate_uuid,
          (SELECT count(*) FROM (
             SELECT pack_id, position FROM public_material_items
             GROUP BY pack_id, position HAVING count(*) > 1
           ) d) AS duplicate_position,
          (SELECT count(*) FROM (
             SELECT pack_id, content_normalized FROM public_material_items
             GROUP BY pack_id, content_normalized HAVING count(*) > 1
           ) d) AS duplicate_content_in_pack,
          (SELECT count(*) FROM public_material_items
           WHERE nullif(btrim(content), '') IS NULL
              OR nullif(btrim(chinese), '') IS NULL
              OR nullif(btrim(card_type), '') IS NULL
              OR nullif(btrim(source_label), '') IS NULL) AS empty_required,
          (SELECT count(*) FROM public_material_items
           WHERE status <> 'approved') AS nonapproved
    """,
}


def main() -> None:
    with engine.connect() as connection:
        database = connection.execute(
            text("SELECT current_database(), current_schema()")
        ).one()
        revision = connection.execute(text("SELECT version_num FROM alembic_version")).one()
        print("database:", tuple(database))
        print("alembic_revision:", revision[0])
        for name, sql in QUERIES.items():
            print(f"\n-- {name}\n{sql.strip()}")
            for row in connection.execute(text(sql)):
                print(tuple(row))


if __name__ == "__main__":
    main()
