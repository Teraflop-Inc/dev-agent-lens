# Downstream Impact Analysis Instructions

## Context

The data pipeline has undergone significant changes. Subagents should analyze what downstream code/stories need updating.

## Recent Changes (Review git log for details)

### Storage Changes
1. **Parquet Export** - New `dev_agent_lens/export/parquet.py` exports JSONL to Parquet with:
   - Two-table design: `{source}_sessions.parquet` and `{source}_spans.parquet`
   - ZSTD compression (96-97% size reduction)
   - Dictionary encoding for high-duplication columns
   - Field deduplication (removes redundant raw_attributes)

2. **Oxen Store Updates** - `dev_agent_lens/storage/oxen_store.py`:
   - Now commits both `unified/` AND `parquet/` directories
   - Remote configured via `~/.dal/config.json` or `OXEN_REMOTE_URL` env var
   - Per-source storage structure (`~/.dal/data/sessions/{source}/`)

3. **Data Locations**:
   - JSONL: `~/.dal/data/unified/{source}_sessions.jsonl` (52+ GB total)
   - Parquet: `~/.dal/data/parquet/{source}_sessions.parquet` (~1.8 GB total)
   - Raw syncs: `~/.dal/data/raw/{source}/sync_*.jsonl`

### CLI Commands Added
- `dal export-parquet --source <name>` - Export JSONL to Parquet
- `dal push` / `dal pull` - Oxen version control
- `dal config oxen-remote <url>` - Configure Oxen remote

## Analysis Tasks for Each Theme

For your assigned theme, do the following:

1. **Read Recent Commits**: Run `git log --oneline -20` and `git diff HEAD~8..HEAD --stat` to understand what changed

2. **Identify Impacted Stories**: Look at which stories in your theme:
   - Reference JSONL file paths (may need Parquet alternatives)
   - Reference storage/OxenStore (may need updates for new structure)
   - Reference query APIs (may need Parquet query support)
   - Have hardcoded paths or assumptions about data layout

3. **Test Current State**: Try running relevant CLI commands or Python APIs to see what works/breaks

4. **Create Linear Ticket**: For each set of related changes needed, create a Linear ticket with:
   - Title: "Update [Story X.Y] for Parquet/Oxen changes"
   - Description: What specifically needs to change and why
   - Parent: Link to the theme issue
   - Team: Engineering

## Theme-Specific Focus Areas

### Theme 2: Query Infrastructure (ENG2-574)
- Does query API read from JSONL or Parquet?
- Should we add Parquet query support (DuckDB)?
- Do export formats need updating?

### Theme 3: Aggregation Framework (ENG2-575)
- Do stats commands read from correct data source?
- Should metrics aggregate from Parquet for performance?
- Token counting with new schema

### Theme 4: LLM Analysis Framework (ENG2-576)
- Batch formatter data source
- Session grouping with new structure

### Theme 5-7: Business/Fabric Integration
- Data access patterns
- Import paths

## Output Format

Create one Linear ticket per significant change area. If a theme needs no changes, report that finding.

Example ticket format:
```
Title: Update Query API to support Parquet data source
Parent: ENG2-574 (Theme 2)
Description:
## Problem
The current query API reads from JSONL files but we now have optimized Parquet exports.

## Changes Needed
1. Add DuckDB/PyArrow query backend option
2. Auto-detect Parquet when available
3. Fall back to JSONL for compatibility

## Acceptance Criteria
- [ ] Query API can read from Parquet
- [ ] 10x+ faster for large datasets
- [ ] Backward compatible with JSONL
```
