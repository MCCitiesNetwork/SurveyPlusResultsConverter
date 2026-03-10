import json
import csv
import sys
import os
import io
import re
from urllib.parse import urlparse
from urllib.request import urlopen, Request


USER_AGENT = "SurveyPlusResultsConverter (github.com/MCCitiesNetwork/SurveyPlusResultsConverter)"


def load_survey(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def to_bbcode(survey, config=None):
    if config is None:
        config = {}

    exclude_ids = set(config.get("exclude_question_ids", []))
    # Preferred: "exclude_question" behaves like simple text-contains
    _exclude_contains_raw = (
        config.get("exclude_question_text_contains")
        or config.get("exclude_question")
        or []
    )
    exclude_contains = [s.lower() for s in _exclude_contains_raw]
    exclude_text_regex = [
        re.compile(p, re.IGNORECASE) for p in config.get("exclude_question_text_regex", [])
    ]
    approval_groups_cfg = config.get("approval_groups", [])
    option_label_overrides = config.get("option_label_overrides", {})
    approval_exclude_abstain = bool(config.get("approval_exclude_abstain"))

    lines = []
    meta = survey["meta"]
    participants = survey.get("participants", "?")

    # Header (title only; omit survey description line)
    lines.append(f"[SIZE=5][COLOR=rgb(41, 105, 176)]{meta['name']}[/COLOR][/SIZE]")
    lines.append(f"[B]Total Respondents: {participants}[/B]")
    lines.append("")

    # First pass: collect data
    # approvals_by_group: title -> list of (question_text, approve_opt, disapprove_opt, abstain_opt)
    approvals_by_group = {g["title"]: [] for g in approval_groups_cfg}
    ungrouped_approvals = []
    generic_groups = {}  # base_text -> {"label": base_text, "options": {name: votes}}
    generic_order = []   # maintain group order
    open_questions = []  # list of (question_text, answers_data)

    for q in survey["questions"]:
        q_id = q.get("id")
        q_text = q.get("text", "")

        # Exclusions
        if q_id in exclude_ids:
            continue
        lower_text = q_text.lower()
        if any(substr in lower_text for substr in exclude_contains):
            continue
        if any(rx.search(q_text) for rx in exclude_text_regex):
            continue

        if q["type"] == "OPEN":
            open_questions.append((q_text, q["answers"]["data"]))
            continue

        if q["type"] != "SELECT":
            continue

        options = q["answers"]["options"]
        labels = {opt["answer"].strip().lower() for opt in options}

        # Approve / Disapprove / Abstain block
        if labels == {"approve", "disapprove", "abstain"}:
            by_label = {opt["answer"].strip().lower(): opt for opt in options}
            record = (
                q_text,
                by_label.get("approve"),
                by_label.get("disapprove"),
                by_label.get("abstain"),
            )

            # Try to assign to a configured approval group.
            # Each pattern in "contains" is treated as a simple
            # case-insensitive "contains" match on the question text.
            # If multiple groups match, the group whose pattern is LONGEST wins
            # (e.g. "President of the Senate" beats generic "President").
            lower_q_text = q_text.lower()
            best_group_title = None
            best_pattern_len = 0

            for group_cfg in approval_groups_cfg:
                raw_patterns = group_cfg.get("contains") or []
                for pat in raw_patterns:
                    pat_str = str(pat)
                    pat_lower = pat_str.lower()
                    if pat_lower and pat_lower in lower_q_text:
                        if len(pat_str) > best_pattern_len:
                            best_pattern_len = len(pat_str)
                            best_group_title = group_cfg["title"]

            if best_group_title is not None:
                approvals_by_group[best_group_title].append(record)
            else:
                ungrouped_approvals.append(record)
        else:
            # Generic groups (party affiliation, residency, etc.)
            base_text = re.sub(r" \(\d+/\d+\)$", "", q_text).strip()
            if base_text not in generic_groups:
                generic_groups[base_text] = {"label": base_text, "options": {}}
                generic_order.append(base_text)

            group = generic_groups[base_text]
            for opt in options:
                label_clean = opt["answer"].strip()
                if label_clean.lower() == "(next page)":
                    # Skip navigation-only option
                    continue
                entry = group["options"].setdefault(label_clean, 0)
                group["options"][label_clean] = entry + opt["votes"]

    # Second pass: render

    # 1) Open questions, each with its own table
    for text, answers in open_questions:
        lines.append(f"[B]{text}[/B]")
        lines.append(f"[I]({len(answers)} responses)[/I]")
        lines.append("[TABLE width=\"100%\"]")
        lines.append("[TR]")
        lines.append("[th][LEFT][COLOR=rgb(84, 172, 210)]User[/COLOR][/LEFT][/th]")
        lines.append("[th][LEFT]Answer[/LEFT][/th]")
        lines.append("[/TR]")
        for entry in answers:
            lines.append(
                "[TR]"
                f"[td width=\"40.0000%\"]{entry['user']}[/td]"
                f"[td width=\"60.0000%\"]{entry['answer']}[/td]"
                "[/TR]"
            )
        lines.append("[/TABLE]")
        lines.append("")

    # 2) Approval ratings tables (all approve/disapprove/abstain questions), grouped if configured
    any_approvals = bool(ungrouped_approvals) or any(approvals_by_group.values())
    if any_approvals:
        lines.append("[SIZE=5][COLOR=rgb(41, 105, 176)]APPROVAL RATINGS[/COLOR][/SIZE]")
        lines.append(f"[B]Total Respondents: {participants}[/B]")
        if approval_exclude_abstain:
            lines.append(
                "[I][COLOR=rgb(209, 213, 216)]"
                "Approval ratings may total 99.99 or 100.01 due to rounding. "
                "Approve and Disapprove percentages are calculated using only non-abstaining responses, "
                "while Abstain is reported as a percentage of all responses."
                "[/COLOR][/I]"
            )
        else:
            lines.append(
                "[I][COLOR=rgb(209, 213, 216)]"
                "Approval ratings may total 99.99 or 100.01 due to rounding."
                "[/COLOR][/I]"
            )
        lines.append("")

        def render_approval_table(title, rows):
            if not rows:
                return
            lines.append("[TABLE width=\"100%\"]")
            lines.append("[TR]")
            # Use group title as first column header
            lines.append(
                "[th width=\"40.0000%\"][LEFT][B][COLOR=rgb(84, 172, 210)]"
                f"{title}"
                "[/COLOR][/B][/LEFT][/th]"
            )
            lines.append("[th width=\"20.0000%\"][LEFT][COLOR=rgb(65, 168, 95)]Approve (%)[/COLOR][/LEFT][/th]")
            lines.append("[th width=\"20.0000%\"][LEFT][COLOR=rgb(209, 72, 65)]Disapprove (%)[/COLOR][/LEFT][/th]")
            lines.append("[th width=\"20.0000%\"][LEFT]Abstain (%)[/LEFT][/th]")
            lines.append("[/TR]")

            for text, approve, disapprove, abstain in rows:
                # Compute percentages, with optional renormalization excluding abstain
                if approve is not None and disapprove is not None and abstain is not None:
                    a_votes = approve.get("votes", 0)
                    d_votes = disapprove.get("votes", 0)
                    ab_votes = abstain.get("votes", 0)
                    total_votes = float(a_votes + d_votes + ab_votes) or 1.0

                    if approval_exclude_abstain and (a_votes + d_votes) > 0:
                        # Approve/Disapprove normalized over non‑abstaining respondents only
                        ad_total = float(a_votes + d_votes)
                        a_pct = 100.0 * a_votes / ad_total
                        d_pct = 100.0 * d_votes / ad_total
                        # Abstain still as share of all respondents
                        ab_pct = 100.0 * ab_votes / total_votes
                    else:
                        # Standard: all three as share of all respondents
                        a_pct = 100.0 * a_votes / total_votes
                        d_pct = 100.0 * d_votes / total_votes
                        ab_pct = 100.0 * ab_votes / total_votes
                else:
                    # Fallback to stored percentages if vote data missing
                    def pct_from_opt(opt):
                        return opt.get("percentage", 0.0) if opt is not None else 0.0

                    a_pct = pct_from_opt(approve)
                    d_pct = pct_from_opt(disapprove)
                    ab_pct = pct_from_opt(abstain)

                lines.append(
                    "[TR]"
                    f"[td width=\"40.0000%\"]{text}[/td]"
                    f"[td width=\"20.0000%\"][COLOR=rgb(65, 168, 95)]{a_pct:.2f}[/COLOR][/td]"
                    f"[td width=\"20.0000%\"][COLOR=rgb(226, 80, 65)]{d_pct:.2f}[/COLOR][/td]"
                    f"[td width=\"20.0000%\"][COLOR=rgb(250, 197, 28)]{ab_pct:.2f}[/COLOR][/td]"
                    "[/TR]"
                )

            lines.append("[/TABLE]")
            lines.append("")

        # Configured groups in order
        for group_cfg in approval_groups_cfg:
            title = group_cfg["title"]
            render_approval_table(title, approvals_by_group.get(title, []))

        # Any remaining approvals without a configured group
        if ungrouped_approvals:
            render_approval_table("Other", ungrouped_approvals)

    # 3) Generic frequency tables (party affiliation, residency, etc.)
    for key in generic_order:
        group = generic_groups[key]
        options = group["options"]
        if not options:
            continue

        # Actual respondents for this grouped question (can be < total participants)
        group_total = sum(options.values())

        lines.append(f"[B]{group['label']}[/B]")
        lines.append(f"[B]Total Respondents: {group_total}[/B]")
        lines.append("[TABLE width=\"100%\"]")
        lines.append("[TR]")
        first_col_label = option_label_overrides.get(group["label"], "Option")
        lines.append(f"[th][LEFT][COLOR=rgb(84, 172, 210)]{first_col_label}[/COLOR][/LEFT][/th]")
        lines.append("[th width=\"39.9803%\"][LEFT]% of respondents[/LEFT][/th]")
        lines.append("[th width=\"19.9407%\"][LEFT]# of respondents[/LEFT][/th]")
        lines.append("[/TR]")

        # Sort by votes desc
        sorted_opts = sorted(options.items(), key=lambda item: item[1], reverse=True)
        for label, votes in sorted_opts:
            pct = (100.0 * float(votes) / group_total) if group_total else 0.0
            lines.append(
                "[TR]"
                f"[td width=\"40.0000%\"]{label}[/td]"
                f"[td width=\"39.9803%\"]{pct:.2f}[/td]"
                f"[td width=\"19.9407%\"]{votes}[/td]"
                "[/TR]"
            )

        lines.append("[/TABLE]")
        lines.append("")

    return "\n".join(lines)

def to_csv(survey, config=None):
    if config is None:
        config = {}

    exclude_ids = set(config.get("exclude_question_ids", []))
    _exclude_contains_raw = (
        config.get("exclude_question_text_contains")
        or config.get("exclude_question")
        or []
    )
    exclude_contains = [s.lower() for s in _exclude_contains_raw]
    exclude_text_regex = [
        re.compile(p, re.IGNORECASE) for p in config.get("exclude_question_text_regex", [])
    ]

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["QuestionID", "Question", "Type", "Option/User", "Answer", "Votes", "Percentage"])

    for q in survey["questions"]:
        q_id = q.get("id")
        q_text = q.get("text", "")

        # Apply same exclusions as BBCode
        if q_id in exclude_ids:
            continue
        lower_text = q_text.lower()
        if any(substr in lower_text for substr in exclude_contains):
            continue
        if any(rx.search(q_text) for rx in exclude_text_regex):
            continue

        if q["type"] == "SELECT":
            sorted_opts = sorted(q["answers"]["options"], key=lambda o: o["votes"], reverse=True)
            for opt in sorted_opts:
                writer.writerow([
                    q["id"], q_text, q["type"],
                    opt["answer"], "", opt["votes"], f"{opt['percentage']:.2f}"
                ])
        elif q["type"] == "OPEN":
            for entry in q["answers"]["data"]:
                writer.writerow([
                    q["id"], q_text, q["type"],
                    entry["user"], entry["answer"], "", ""
                ])

    return buf.getvalue()

def load_config_from_path(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except OSError as e:
        print(f"Warning: could not read config file {path}: {e}", file=sys.stderr)
    except json.JSONDecodeError as e:
        print(f"Warning: invalid JSON in config file {path}: {e}", file=sys.stderr)
    return {}


def load_config_by_name(name, configs_dir="configs"):
    if not os.path.isdir(configs_dir):
        print(f"Warning: configs directory not found: {configs_dir}", file=sys.stderr)
        return {}

    for root, _, files in os.walk(configs_dir):
        for fname in files:
            if not fname.lower().endswith(".json"):
                continue
            path = os.path.join(root, fname)
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                continue
            if data.get("name") == name:
                print(f"Using config file: {path}")
                return data

    print(f"Warning: no config under '{configs_dir}' has name='{name}'", file=sys.stderr)
    return {}


def main():
    if len(sys.argv) < 2:
        print("Usage: python convert.py <survey.json or URL> [--config=path.json] [--config-name=\"Friendly name\"]")
        print("  (always writes both <name>.bbcode.txt and <name>.csv)")
        sys.exit(1)

    input_arg = sys.argv[1]
    extra_args = sys.argv[2:]

    # Optional JSON configuration: --config=path/to/config.json or --config-name=FriendlyName
    config_path = None
    config_name = None
    for arg in extra_args:
        if arg.startswith("--config="):
            config_path = arg.split("=", 1)[1]
        elif arg.startswith("--config-name="):
            config_name = arg.split("=", 1)[1]

    # Load configuration, if provided
    if config_path:
        config = load_config_from_path(config_path)
    elif config_name:
        config = load_config_by_name(config_name)
    else:
        config = {}

    # Determine if input is a URL or local file path
    is_url = input_arg.startswith("http://") or input_arg.startswith("https://")

    if is_url:
        # Derive a sensible base name from URL path
        parsed = urlparse(input_arg)
        filename = os.path.basename(parsed.path) or "survey.json"
        base = os.path.splitext(filename)[0]

        try:
            req = Request(input_arg, headers={"User-Agent": USER_AGENT})
            with urlopen(req) as resp:
                raw_data = resp.read().decode("utf-8")
            survey = json.loads(raw_data)
        except Exception as e:
            print(f"Error: failed to download or parse JSON from URL {input_arg}: {e}", file=sys.stderr)
            sys.exit(1)
    else:
        path = input_arg
        if not os.path.isfile(path):
            print(f"Error: file not found: {path}")
            sys.exit(1)
        survey = load_survey(path)
        base = os.path.splitext(path)[0]

    # Always write both files
    bbcode_path = base + ".bbcode.txt"
    csv_path = base + ".csv"
    with open(bbcode_path, "w", encoding="utf-8") as f:
        f.write(to_bbcode(survey, config=config))
    with open(csv_path, "w", encoding="utf-8", newline="") as f:
        f.write(to_csv(survey, config=config))
    print(f"Wrote {bbcode_path}")
    print(f"Wrote {csv_path}")

    # No optional flags: do not print BBCode/CSV to stdout

if __name__ == "__main__":
    main()
