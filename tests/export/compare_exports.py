#!/usr/bin/env python3
"""
Cross-Pipeline Export Comparison Script

Compares Claude and LiteLLM markdown exports after:
1. Stripping PIPELINE_SPECIFIC sections
2. Filtering Claude-only messages (exit commands, local stdout, caveats)
3. Extracting sections for order-independent comparison

Usage:
    python compare_exports.py <claude_export_dir> <litellm_export_dir>

Example:
    python compare_exports.py \
        /tmp/markdown_negotiation/claude_exports/1f3e47ff_subagent \
        /tmp/markdown_negotiation/litellm_exports/1f3e47ff_subagent
"""

import argparse
import re
import sys
from pathlib import Path


def strip_pipeline_specific(content: str) -> str:
    """Remove PIPELINE_SPECIFIC sections."""
    pattern = r"<!-- BEGIN PIPELINE_SPECIFIC -->.*?<!-- END PIPELINE_SPECIFIC -->"
    return re.sub(pattern, "", content, flags=re.DOTALL)


def filter_claude_only_messages(content: str) -> str:
    """Filter messages that only appear in Claude exports.

    Claude JSONL captures ALL message types including:
    - System caveats ("Caveat: The messages below...")
    - User commands (/clear, /exit, etc.)
    - Local stdout/stderr
    - Terminal interaction messages

    Phoenix/LiteLLM only captures LLM call inputs/outputs, so these
    non-LLM messages don't exist in that pipeline.

    This is a FUNDAMENTAL DIFFERENCE between the pipelines and must be
    handled in comparison to avoid false ordering mismatches.
    """
    lines = content.split("\n")
    filtered_lines = []
    skip_until_next_section = False

    i = 0
    while i < len(lines):
        line = lines[i]

        # Check if this is a User section that should be filtered
        if line.strip() == "### User":
            # Look ahead to see what content follows
            content_preview = ""
            for j in range(i + 1, min(i + 10, len(lines))):
                if lines[j].strip() and not lines[j].strip() == "---":
                    content_preview = lines[j].strip()
                    break

            # Skip Claude-only messages
            if any([
                content_preview.startswith("Caveat:"),
                content_preview.startswith("<command-name>"),
                content_preview.startswith("<local-command-"),
                "DO NOT respond to these messages" in content_preview,
            ]):
                # Skip this entire section until next ### or ---
                skip_until_next_section = True
                i += 1
                continue

        # If we're skipping, check for section end
        if skip_until_next_section:
            if line.strip() == "---":
                skip_until_next_section = False
                i += 1
                continue
            elif line.startswith("### "):
                skip_until_next_section = False
                # Don't skip this line - it's the start of a new section
            else:
                i += 1
                continue

        filtered_lines.append(line)
        i += 1

    return "\n".join(filtered_lines)


def normalize_for_comparison(content: str, is_claude: bool = False) -> str:
    """Normalize content for comparison.

    Filters system-generated messages (Caveat, /clear commands, stdout) from
    BOTH pipelines since these don't represent actual user turns in the
    LLM conversation.
    """
    content = strip_pipeline_specific(content)
    # Filter system-generated messages from BOTH pipelines
    # LiteLLM captures these from input arrays, Claude has them in JSONL
    content = filter_claude_only_messages(content)
    # Remove extra whitespace from filtering
    content = re.sub(r"\n{3,}", "\n\n", content)
    return content.strip()


def extract_sections(content: str) -> set[str]:
    """Extract individual sections for order-independent comparison."""
    # Split by section headers (### User, ### Assistant, ### Tool:, etc.)
    sections = re.split(r"\n(?=### )", content)
    return set(s.strip() for s in sections if s.strip())


def extract_section_headers(content: str) -> list[str]:
    """Extract just section header types (User, Assistant, Tool:X, Subagent:X)."""
    headers = re.findall(r"^### (User|Assistant|Tool: \w+|Subagent: \w+|Parallel Tools)", content, re.MULTILINE)
    return headers


def extract_section_sequence(content: str) -> list[str]:
    """Extract ordered list of normalized section types for ordering comparison."""
    # Match all ### headers
    headers = re.findall(r"^### (.+)$", content, re.MULTILINE)
    normalized = []
    for h in headers:
        if h.startswith("Tool:"):
            normalized.append("Tool")
        elif h.startswith("Subagent:"):
            normalized.append("Subagent")
        elif h.startswith("Compaction"):
            normalized.append("Compaction")
        elif h.startswith("Parallel Tools"):
            normalized.append("Parallel Tools")
        else:
            normalized.append(h)  # User, Assistant
    return normalized


def lcs_length(a: list[str], b: list[str]) -> int:
    """Compute Longest Common Subsequence length between two sequences.

    LCS measures how many elements appear in the same relative order,
    even if they're not at the exact same positions.
    """
    m, n = len(a), len(b)
    dp = [[0] * (n + 1) for _ in range(m + 1)]
    for i in range(1, m + 1):
        for j in range(1, n + 1):
            if a[i-1] == b[j-1]:
                dp[i][j] = dp[i-1][j-1] + 1
            else:
                dp[i][j] = max(dp[i-1][j], dp[i][j-1])
    return dp[m][n]


# LCS accuracy threshold for ordering check (80% = sections in same relative order)
LCS_ORDERING_THRESHOLD = 0.80


def compare_ordering(claude_seq: list[str], litellm_seq: list[str]) -> tuple[bool, dict]:
    """Compare section ordering between exports using LCS-based accuracy.

    Uses Longest Common Subsequence to measure relative ordering accuracy.
    Passes if LCS accuracy >= LCS_ORDERING_THRESHOLD (80%).

    Returns:
        (matches, details) where details contains ordering info
    """
    details = {
        "claude_length": len(claude_seq),
        "litellm_length": len(litellm_seq),
        "first_mismatch_index": None,
        "first_mismatch": None,
        "matches": False,
        "lcs_length": 0,
        "lcs_accuracy": 0.0,
        "threshold": LCS_ORDERING_THRESHOLD,
    }

    # Exact match is best case
    if claude_seq == litellm_seq:
        details["matches"] = True
        details["lcs_length"] = len(claude_seq)
        details["lcs_accuracy"] = 1.0
        return True, details

    # Calculate LCS-based accuracy
    lcs = lcs_length(claude_seq, litellm_seq)
    # Use the longer sequence as denominator for conservative accuracy
    max_len = max(len(claude_seq), len(litellm_seq))
    lcs_accuracy = lcs / max_len if max_len > 0 else 0.0

    details["lcs_length"] = lcs
    details["lcs_accuracy"] = lcs_accuracy

    # Pass if LCS accuracy meets threshold
    if lcs_accuracy >= LCS_ORDERING_THRESHOLD:
        details["matches"] = True
        return True, details

    # Find first mismatch for diagnostic info
    min_len = min(len(claude_seq), len(litellm_seq))
    for i in range(min_len):
        if claude_seq[i] != litellm_seq[i]:
            details["first_mismatch_index"] = i
            details["first_mismatch"] = {
                "claude": claude_seq[i],
                "litellm": litellm_seq[i],
                "context_claude": claude_seq[max(0,i-2):i+3],
                "context_litellm": litellm_seq[max(0,i-2):i+3],
            }
            break
    else:
        # No mismatch in common prefix, difference is in length
        details["first_mismatch_index"] = min_len
        details["first_mismatch"] = {
            "claude": claude_seq[min_len] if len(claude_seq) > min_len else "<END>",
            "litellm": litellm_seq[min_len] if len(litellm_seq) > min_len else "<END>",
        }

    return False, details


def count_section_types(content: str) -> dict[str, int]:
    """Count occurrences of each section type."""
    headers = extract_section_headers(content)
    counts: dict[str, int] = {}
    for h in headers:
        # Normalize tool/subagent names
        if h.startswith("Tool:"):
            key = "Tool"
        elif h.startswith("Subagent:"):
            key = "Subagent"
        else:
            key = h
        counts[key] = counts.get(key, 0) + 1
    return counts


def compare_main_files(
    claude_path: Path, litellm_path: Path, verbose: bool = False
) -> tuple[bool, dict]:
    """Compare main session files.

    Per SUPERVISOR_RESPONSE_TO_AGENT_B.md, approved tolerances:
    1. Message ordering - allowed to differ
    2. Exit/local messages - filtered from Claude
    3. Turn count - may differ by 1-2 due to interrupt deduplication

    Returns:
        (passed, details) where details contains comparison info
    """
    details = {
        "claude_path": str(claude_path),
        "litellm_path": str(litellm_path),
        "claude_section_counts": {},
        "litellm_section_counts": {},
        "exact_match": False,
        "structural_match": False,
        "tolerance_issues": [],
    }

    if not claude_path.exists():
        details["error"] = f"Claude file not found: {claude_path}"
        return False, details

    if not litellm_path.exists():
        details["error"] = f"LiteLLM file not found: {litellm_path}"
        return False, details

    claude_raw = claude_path.read_text()
    litellm_raw = litellm_path.read_text()

    claude_normalized = normalize_for_comparison(claude_raw, is_claude=True)
    litellm_normalized = normalize_for_comparison(litellm_raw, is_claude=False)

    # First try exact match
    if claude_normalized == litellm_normalized:
        details["exact_match"] = True
        details["structural_match"] = True
        details["ordering_match"] = True
        return True, details

    # Check ordering (sequence of section types)
    claude_seq = extract_section_sequence(claude_normalized)
    litellm_seq = extract_section_sequence(litellm_normalized)
    ordering_match, ordering_details = compare_ordering(claude_seq, litellm_seq)
    details["ordering_match"] = ordering_match
    details["ordering_details"] = ordering_details

    # Count section types (order-independent, content-independent)
    claude_counts = count_section_types(claude_normalized)
    litellm_counts = count_section_types(litellm_normalized)

    # Count compactions in Claude export (these add extra Assistant turns)
    compaction_count = len(re.findall(r"^### Compaction #\d+", claude_normalized, re.MULTILINE))

    details["claude_section_counts"] = claude_counts
    details["litellm_section_counts"] = litellm_counts
    details["claude_compaction_count"] = compaction_count

    # Check structural compatibility with approved tolerances
    all_keys = set(claude_counts.keys()) | set(litellm_counts.keys())
    issues = []

    for key in all_keys:
        claude_count = claude_counts.get(key, 0)
        litellm_count = litellm_counts.get(key, 0)
        diff = abs(claude_count - litellm_count)

        if key == "User":
            # User turns may differ by up to 2 (interrupt deduplication + variance)
            # Plus compaction count (compaction summaries are user messages)
            tolerance = 2 + compaction_count
            if diff > tolerance:
                issues.append(f"User turns differ by {diff} (tolerance: {tolerance}, includes {compaction_count} compactions)")
        elif key == "Tool":
            # Tool calls should match exactly
            if diff > 0:
                issues.append(f"Tool calls differ: Claude={claude_count}, LiteLLM={litellm_count}")
        elif key == "Subagent":
            # Subagent count should match exactly
            if diff > 0:
                issues.append(f"Subagent count differs: Claude={claude_count}, LiteLLM={litellm_count}")
        elif key == "Assistant":
            # Assistant turns may differ slightly due to message grouping
            # Plus compaction count (each compaction adds an assistant resume turn)
            tolerance = 2 + compaction_count
            if diff > tolerance:
                issues.append(f"Assistant turns differ by {diff} (tolerance: {tolerance}, includes {compaction_count} compaction resumes)")
        else:
            # Unknown section type (including "Compaction" which is Claude-only)
            if key == "Compaction":
                # Compaction sections are expected in Claude only
                pass
            elif diff > 0:
                issues.append(f"{key} count differs: Claude={claude_count}, LiteLLM={litellm_count}")

    details["tolerance_issues"] = issues
    details["structural_match"] = len(issues) == 0

    if verbose:
        print("\n=== SECTION COUNTS ===")
        print(f"Claude:  {claude_counts}")
        print(f"LiteLLM: {litellm_counts}")
        if compaction_count > 0:
            print(f"Claude compactions: {compaction_count} (adjusts User/Assistant tolerance)")
        if issues:
            print("\n=== TOLERANCE ISSUES ===")
            for issue in issues:
                print(f"  ⚠️  {issue}")

    # Both structural match AND ordering must pass
    passed = len(issues) == 0 and ordering_match
    return passed, details


def compare_subagent_files(
    claude_dir: Path, litellm_dir: Path, verbose: bool = False
) -> tuple[bool, list[dict]]:
    """Compare subagent files between directories.

    Note: Subagent content may differ (LiteLLM ~20% summary-only case).
    We check structure but allow content differences.
    """
    claude_subagents = sorted(claude_dir.glob("subagent_*.md"))
    litellm_subagents = sorted(litellm_dir.glob("subagent_*.md"))

    claude_names = {f.name for f in claude_subagents}
    litellm_names = {f.name for f in litellm_subagents}

    results = []

    # Check for missing files
    only_claude = claude_names - litellm_names
    only_litellm = litellm_names - claude_names

    if only_claude:
        results.append({
            "status": "warning",
            "message": f"Subagent files only in Claude: {only_claude}",
        })
    if only_litellm:
        results.append({
            "status": "warning",
            "message": f"Subagent files only in LiteLLM: {only_litellm}",
        })

    # Compare common files (structure check)
    common = claude_names & litellm_names
    for name in sorted(common):
        claude_file = claude_dir / name
        litellm_file = litellm_dir / name

        claude_content = claude_file.read_text()
        litellm_content = litellm_file.read_text()

        # Check structural elements
        checks = [
            ("# Subagent:" in claude_content and "# Subagent:" in litellm_content, "Header"),
            ("## Context" in claude_content and "## Context" in litellm_content, "Context section"),
            ("## Task Prompt" in claude_content and "## Task Prompt" in litellm_content, "Task Prompt section"),
            ("## Conversation" in claude_content and "## Conversation" in litellm_content, "Conversation section"),
            ("PIPELINE_SPECIFIC" in claude_content and "PIPELINE_SPECIFIC" in litellm_content, "Pipeline markers"),
        ]

        failed_checks = [check_name for passed, check_name in checks if not passed]
        if failed_checks:
            results.append({
                "status": "fail",
                "file": name,
                "message": f"Missing structural elements: {failed_checks}",
            })
        else:
            results.append({
                "status": "pass",
                "file": name,
                "message": "Structure matches (content may differ)",
            })

    # Pass if no structural failures in common files
    # Naming differences (only_claude/only_litellm) are warnings, not failures
    # The main file's Subagent count is the authoritative check
    has_failures = any(r["status"] == "fail" for r in results)
    passed = not has_failures
    return passed, results


def main():
    parser = argparse.ArgumentParser(
        description="Compare Claude and LiteLLM markdown exports"
    )
    parser.add_argument("claude_dir", type=Path, help="Claude export directory")
    parser.add_argument("litellm_dir", type=Path, help="LiteLLM export directory")
    parser.add_argument("-v", "--verbose", action="store_true", help="Show detailed diff")
    args = parser.parse_args()

    if not args.claude_dir.exists():
        print(f"❌ Claude directory not found: {args.claude_dir}")
        sys.exit(1)
    if not args.litellm_dir.exists():
        print(f"❌ LiteLLM directory not found: {args.litellm_dir}")
        sys.exit(1)

    # Find main session file (UUID.md pattern)
    claude_main_files = list(args.claude_dir.glob("????????-????-????-????-????????????.md"))
    litellm_main_files = list(args.litellm_dir.glob("????????-????-????-????-????????????.md"))

    if not claude_main_files:
        print(f"❌ No main session file found in Claude export: {args.claude_dir}")
        sys.exit(1)
    if not litellm_main_files:
        print(f"❌ No main session file found in LiteLLM export: {args.litellm_dir}")
        sys.exit(1)

    claude_main = claude_main_files[0]
    litellm_main = litellm_main_files[0]

    print(f"Comparing exports:")
    print(f"  Claude:  {claude_main}")
    print(f"  LiteLLM: {litellm_main}")
    print()

    # Compare main files
    print("=== MAIN FILE COMPARISON ===")
    main_passed, main_details = compare_main_files(claude_main, litellm_main, args.verbose)

    if main_details.get("exact_match"):
        print("✅ EXACT MATCH (after normalization)")
    else:
        # Show structural match status
        if main_details.get("structural_match"):
            print("✅ Structure: matches within tolerances")
            print(f"   Claude:  {main_details['claude_section_counts']}")
            print(f"   LiteLLM: {main_details['litellm_section_counts']}")
        else:
            if main_details.get("error"):
                print(f"❌ Structure: {main_details['error']}")
            else:
                print("❌ Structure: mismatch exceeds tolerances")
                print(f"   Claude:  {main_details['claude_section_counts']}")
                print(f"   LiteLLM: {main_details['litellm_section_counts']}")
                for issue in main_details.get("tolerance_issues", []):
                    print(f"   ⚠️  {issue}")

        # Show ordering match status (LCS-based)
        ord_details = main_details.get("ordering_details", {})
        lcs_accuracy = ord_details.get("lcs_accuracy", 0)
        threshold = ord_details.get("threshold", 0.80)
        if main_details.get("ordering_match"):
            print(f"✅ Ordering: {lcs_accuracy*100:.1f}% LCS accuracy (threshold: {threshold*100:.0f}%)")
        else:
            print(f"❌ Ordering: {lcs_accuracy*100:.1f}% LCS accuracy (threshold: {threshold*100:.0f}%)")
            mismatch = ord_details.get("first_mismatch", {})
            if mismatch:
                print(f"   First mismatch at index {ord_details.get('first_mismatch_index', '?')}")
                print(f"   Claude:  {mismatch.get('claude', '?')}")
                print(f"   LiteLLM: {mismatch.get('litellm', '?')}")

    # Compare subagent files
    print()
    print("=== SUBAGENT FILE COMPARISON ===")
    subagent_passed, subagent_results = compare_subagent_files(
        args.claude_dir, args.litellm_dir, args.verbose
    )

    if not subagent_results:
        print("ℹ️  No subagent files to compare")
    else:
        for r in subagent_results:
            status_icon = {"pass": "✅", "warning": "⚠️", "fail": "❌"}[r["status"]]
            if "file" in r:
                print(f"{status_icon} {r['file']}: {r['message']}")
            else:
                print(f"{status_icon} {r['message']}")

    # Overall result
    print()
    print("=== OVERALL RESULT ===")
    if main_passed and subagent_passed:
        print("✅ PASS: Exports are compatible")
        sys.exit(0)
    else:
        print("❌ FAIL: Exports have incompatibilities")
        sys.exit(1)


if __name__ == "__main__":
    main()
