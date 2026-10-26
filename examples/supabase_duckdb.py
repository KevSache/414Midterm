"""Read a few markets by attaching Supabase to DuckDB, read-only."""

import duckdb

con = duckdb.connect()
con.execute("INSTALL postgres; LOAD postgres;")
con.execute("""
    ATTACH 'host=aws-0-us-west-2.pooler.supabase.com port=6543 dbname=postgres
             user=polymarketdatabase.merbqatlxbrcvtbnuaqe password=PolymarketData2026! sslmode=require'
    AS pm (TYPE postgres, READ_ONLY);
""")

result = con.execute(
    "SELECT slug, title, best_bid, best_ask FROM pm.markets LIMIT 10"
)
columns = [col[0] for col in result.description]
print("\t".join(columns))
for row in result.fetchall():
    print("\t".join("" if value is None else str(value) for value in row))
