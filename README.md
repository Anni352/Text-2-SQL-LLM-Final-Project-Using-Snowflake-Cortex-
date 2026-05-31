# Text-to-SQL on Snowflake (Snowflake Cortex)

A Streamlit application that turns plain-English questions into validated Snowflake SQL, runs them, and returns results, summaries, and follow-up questions. All LLM inference happens **inside Snowflake** using Snowflake Cortex, so no data or schema ever leaves the warehouse environment.

Beyond querying, the app includes a data-quality test runner and a source-to-target table diff tool with root-cause analysis, making it useful for day-to-day data engineering, not just ad-hoc questions.

---

## Why Cortex (the design decision)

Most text-to-SQL demos send your schema and sometimes your data to an external LLM API. That is a data-governance problem in any enterprise setting. This project uses `AI_COMPLETE` / `SNOWFLAKE.CORTEX.COMPLETE` so inference runs against models hosted within your Snowflake account. Nothing leaves the perimeter, which makes the approach viable where external API calls would be blocked.

---

## Features

- **Natural language to SQL** using Snowflake Cortex with structured JSON output (`AI_COMPLETE` with a JSON schema, falling back to `CORTEX.COMPLETE`).
- **Read-only safety layer** that blocks DDL/DML (`CREATE`, `DROP`, `INSERT`, `UPDATE`, `DELETE`, `MERGE`, etc.), rejects multi-statement input, and only allows queries beginning with `WITH` or `SELECT`.
- **Results, summaries, and auto follow-ups** so a non-technical user can explore data without writing SQL.
- **Chat history** in the sidebar; click any past question to replay its full output.
- **Data quality (QA) page**: row-count, all-null-column, and primary-key uniqueness checks, plus custom natural-language test prompts run in batch with PASS/FAIL/ERROR classification.
- **Table diff page**: compare a target table against a source query using symmetric `MINUS` (A-B and B-A), a labeled union view, and a per-column reason (RCA) tab to localize mismatches.
- **Credential safety**: Snowflake credentials are never written to disk. Only UI state is persisted, encrypted, and restored on next login with the same credentials.

---

## Architecture

```
text-to-sql-snowflake/
├── app.py                      # Streamlit UI and page orchestration
├── config/
│   └── system_prompt.txt       # System prompt for SQL generation
├── core/
│   ├── llm_handler.py          # Cortex calls (AI_COMPLETE + COMPLETE fallback), JSON parsing
│   ├── sql_generator.py        # NL -> SQL, identifier normalization, fencing
│   ├── query_runner.py         # Safety validation + execution -> DataFrame
│   ├── visualization.py        # Result summaries
│   ├── qa_tests.py             # Built-in and custom data-quality tests
│   └── diff_tools.py           # Source/target diffs, MINUS queries, RCA
├── utils/
│   ├── snowflake_connector.py  # Session-scoped Snowflake connection + context
│   └── helpers.py              # Identifier quoting, encrypted KV store, SQL extraction
├── requirements.txt
└── README.md
```

### Request flow

1. User asks a question on the main page.
2. `sql_generator` normalizes table mentions and prompts Cortex via `llm_handler`.
3. Cortex returns structured JSON (`sql` + `explanation`); the SQL is extracted and fenced.
4. `query_runner.validate_safe_sql` enforces read-only, single-statement rules before execution.
5. Results render as a table with a summary, and Cortex proposes follow-up questions.

---

## Setup

Requires Python 3.10+ and a Snowflake account with Cortex enabled in a supported region.

```bash
pip install -r requirements.txt
streamlit run app.py
```

Set the model in a `.env` file (defaults to `claude-3-5-sonnet` if unset):

```
CORTEX_MODEL=claude-3-5-sonnet
```

On launch:

1. **Login**: enter Snowflake Account, Username, Password.
2. **Context**: set Warehouse, Database, Schema.
3. **Ask**: type a question, review the generated SQL, results, summary, and follow-ups.

---

## Identifier rules

To keep table resolution unambiguous, identifiers are interpreted as either a bare `TABLE` or a fully qualified `DATABASE.SCHEMA.TABLE`. Two-part `SCHEMA.TABLE` names are intentionally collapsed to the table name during NL parsing so a prior table is never reinterpreted as a schema.

---

## Security notes

- Credentials are held only in the Snowflake session and are never persisted. The encrypted local store (`.store/`) holds UI state only, keyed by a PBKDF2-derived (390k iterations) Fernet key.
- The query path is read-only by construction; write and DDL statements are rejected before execution.
- Built for single-user, own-credential use. The QA and diff utilities interpolate user-supplied table names into SQL; treat them as trusted-input tools rather than a multi-tenant service.

---

## Roadmap

- **Inject live schema into the generation prompt.** The column-introspection plumbing already exists (`get_table_columns`, `_describe_columns`); wiring it into `schema_hint` at generation time will ground the model in real table structures and improve accuracy.
- **Parameterize / quote all identifiers** in the QA and `INFORMATION_SCHEMA` paths to remove injection surface.
- **Key-pair or SSO auth** as an alternative to password login.
- **Query cost and row-scan guardrails** (`LIMIT` injection, byte-scan caps) before execution.
- **Evaluation harness**: a fixed question/expected-SQL set to measure generation accuracy over time.

---

## Tech stack

Snowflake Cortex · Snowflake · Python · SQL · Streamlit · pandas · cryptography
