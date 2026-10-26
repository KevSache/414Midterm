"""Read a few markets from Supabase with pandas and SQLAlchemy."""

import pandas as pd
from sqlalchemy import create_engine

engine = create_engine(
    "postgresql+psycopg2://polymarketdatabase.merbqatlxbrcvtbnuaqe:PolymarketData2026!"
    "@aws-0-us-west-2.pooler.supabase.com:6543/postgres",
    connect_args={"sslmode": "require"},
)

frame = pd.read_sql(
    "SELECT slug, title, best_bid, best_ask FROM markets LIMIT 10",
    engine,
)
print(frame.to_string(index=False))
