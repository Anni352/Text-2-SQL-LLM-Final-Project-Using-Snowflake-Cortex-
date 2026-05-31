from tabulate import tabulate

def summarize_df(df):
    if df is None:
        return "No data."
    return f"{len(df):,} rows • {len(df.columns)} columns"
