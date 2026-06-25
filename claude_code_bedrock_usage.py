#!/usr/bin/env python3
"""Claude Code on AWS Bedrock — tokens used, spend, and budget."""

import argparse
import sys
import unicodedata
from datetime import datetime, timezone, timedelta
import boto3
from botocore.exceptions import BotoCoreError, ClientError

# ANSI colors
R       = "\033[0m"
BOLD    = "\033[1m"
CYAN    = "\033[96m"
GREEN   = "\033[92m"
YELLOW  = "\033[93m"
MAGENTA = "\033[95m"
BLUE    = "\033[94m"
RED     = "\033[91m"

def c(color, text): return f"{color}{text}{R}"

def die(msg):
    print(f"\n{c(RED + BOLD, 'Error:')} {msg}\n", file=sys.stderr)
    sys.exit(1)

def main():
    parser = argparse.ArgumentParser(
        description="Claude Code on AWS Bedrock — monthly usage summary",
        epilog="Example: %(prog)s --profile my-aws-profile",
    )
    parser.add_argument("--profile", required=True, help="AWS profile name")
    parser.add_argument("--region", default="eu-central-1", help="AWS region (default: eu-central-1)")
    parser.add_argument("--budget", type=float, default=None, help="Override total budget in USD (skips AWS Budgets lookup)")
    parser.add_argument("--month-year", default=None, metavar="MM-YYYY", dest="month", help="Month and year to query, e.g. 06-2026 (defaults to current month, cannot be in the future)")
    args = parser.parse_args()

    try:
        session = boto3.Session(profile_name=args.profile, region_name=args.region)
    except Exception as e:
        die(f"Could not create AWS session for profile '{args.profile}': {e}")

    now = datetime.now(timezone.utc)

    if args.month:
        try:
            target = datetime.strptime(args.month, "%m-%Y").replace(tzinfo=timezone.utc)
        except ValueError:
            die(f"Invalid --month format '{args.month}'. Expected MM-YYYY, e.g. 06-2026")
        if target.year > now.year or (target.year == now.year and target.month > now.month):
            die(f"--month {args.month} is in the future. Only current or past months are allowed.")
        start = target.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        if target.year == now.year and target.month == now.month:
            period_end   = now
            display_end  = now
        else:
            # API end: 1st of next month (exclusive); display end: last day of queried month
            if target.month == 12:
                period_end = target.replace(year=target.year + 1, month=1, day=1)
            else:
                period_end = target.replace(month=target.month + 1, day=1)
            display_end = period_end - timedelta(days=1)
    else:
        start       = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        period_end  = now
        display_end = now

    # If start == period_end (e.g. run exactly on the 1st at midnight), nudge start back
    if start >= period_end:
        start = period_end - timedelta(minutes=1)

    # --- Tokens Used (CloudWatch) — month-to-date ---
    try:
        cw = session.client("cloudwatch")

        def get_metric_sum(metric_name):
            resp = cw.get_metric_statistics(
                Namespace="AWS/Bedrock",
                MetricName=metric_name,
                StartTime=start,
                EndTime=period_end,
                Period=max(int((period_end - start).total_seconds() // 60) * 60, 60),
                Statistics=["Sum"],
            )
            pts = resp.get("Datapoints", [])
            return int(pts[0]["Sum"]) if pts else 0

        input_tokens  = get_metric_sum("InputTokenCount")
        output_tokens = get_metric_sum("OutputTokenCount")
        total_tokens_used = input_tokens + output_tokens
    except (BotoCoreError, ClientError) as e:
        die(f"CloudWatch error: {e}")

    # --- Total Spent (Cost Explorer) ---
    try:
        ce = session.client("ce", region_name="us-east-1")
        ce_start = start.strftime("%Y-%m-%d")
        ce_end   = period_end.strftime("%Y-%m-%d")
        # Cost Explorer requires Start < End
        if ce_start == ce_end:
            ce_end = (period_end + timedelta(days=1)).strftime("%Y-%m-%d")
        ce_resp = ce.get_cost_and_usage(
            TimePeriod={"Start": ce_start, "End": ce_end},
            Granularity="MONTHLY",
            GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}],
            Metrics=["UnblendedCost"],
        )
        bedrock_costs = {}
        for r in ce_resp.get("ResultsByTime", []):
            for g in r["Groups"]:
                svc = g["Keys"][0]
                if "bedrock edition" in svc.lower() or "amazon bedrock" in svc.lower():
                    bedrock_costs[svc] = bedrock_costs.get(svc, 0) + float(g["Metrics"]["UnblendedCost"]["Amount"])
        total_spent = sum(bedrock_costs.values())
    except (BotoCoreError, ClientError) as e:
        die(f"Cost Explorer error: {e}")

    # --- Total Budget (AWS Budgets or CLI override) ---
    try:
        if args.budget is not None:
            total_budget = args.budget
        else:
            account_id   = session.client("sts").get_caller_identity()["Account"]
            budgets_resp = session.client("budgets").describe_budgets(AccountId=account_id)
            total_budget = sum(
                float(b["BudgetLimit"]["Amount"])
                for b in budgets_resp.get("Budgets", [])
                if "BudgetLimit" in b
            )
    except (BotoCoreError, ClientError) as e:
        die(f"Budgets error: {e}")

    spend_pct   = (total_spent / total_budget * 100) if total_budget else 0
    spend_color = RED if spend_pct >= 80 else YELLOW if spend_pct >= 50 else GREEN

    W   = 60
    BAR = 30
    border = c(CYAN, "═" * W)
    thin   = c(CYAN, "─" * W)

    def bar(filled, total, color):
        filled = max(0, min(filled, total))
        return c(color, "█" * filled) + c(R + BOLD, "▒" * (total - filled))

    def emoji_width(s):
        return sum(2 if unicodedata.east_asian_width(ch) in ('W', 'F') or ord(ch) > 0x1F300 else 1 for ch in s)

    def row(icon, label, value, vcolor=BOLD):
        label_str   = f"  {icon}  {label}"
        display_len = emoji_width(label_str) + len(value)
        spaces      = W - display_len
        return f"{label_str}{' ' * max(spaces, 1)}{c(vcolor, value)}"

    print(f"\n{border}")
    print(c(BOLD + CYAN, f"  🤖  Claude Code on AWS Bedrock — Monthly Usage Summary".center(W)))
    print(thin)
    print(f"  👤  AWS Profile : {c(BOLD, args.profile)}  │  🌍 Region : {c(BOLD, args.region)}")
    print(f"  📅  Period      : {c(BOLD, start.strftime('%d %b %Y'))}  →  {c(BOLD, display_end.strftime('%d %b %Y'))}")
    print(border)

    print(c(BOLD + YELLOW, f"\n  🔐  Tokens\n"))
    print(row("🔓", "Tokens Used (Total) ", f"{total_tokens_used:,}",  GREEN))
    print(row("     └─", "🔑  Input Tokens        ", f"{input_tokens:,}",  BLUE))
    print(row("     └─", "🔑  Output Tokens       ", f"{output_tokens:,}", MAGENTA))

    print(c(BOLD + YELLOW, f"\n  💰  Money\n"))
    print(row("💸", "Total Spent         ", f"${total_spent:,.2f}",   spend_color))
    for svc, amt in sorted(bedrock_costs.items(), key=lambda x: -x[1]):
        short = svc.replace(" (Amazon Bedrock Edition)", "")
        print(row("     └─", f"🧠  {short:<24}", f"${amt:,.2f}", spend_color))
    print(row("🏦", "Total Budget            ", f"${total_budget:,.2f}",  CYAN))
    spend_filled = int(spend_pct / 100 * BAR)
    print(f"\n  💹  {bar(spend_filled, BAR, spend_color)}  {c(spend_color + BOLD, f'{spend_pct:.2f}%')} of budget used")

    print(f"\n{border}\n")


if __name__ == "__main__":
    main()
