"""Read a few markets from Supabase with psycopg2.

The username is polymarketdatabase.merbqatlxbrcvtbnuaqe. The suffix
is part of the username.
"""

import psycopg2

con = psycopg2.connect(
    "postgresql://polymarketdatabase.merbqatlxbrcvtbnuaqe:PolymarketData2026!"
    "@aws-0-us-west-2.pooler.supabase.com:6543/postgres?sslmode=require"
)
cur = con.cursor()
cur.execute("SELECT slug, title, best_bid, best_ask FROM markets LIMIT 10;")
for row in cur.fetchall():
    print(row)
cur.close()
con.close()
