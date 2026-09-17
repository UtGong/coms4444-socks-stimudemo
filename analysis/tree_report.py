"""Generate an HTML report from a completed or partially completed tree database."""

from __future__ import annotations

import argparse
import html
import sqlite3
from pathlib import Path


STYLE = """
:root{font-family:Inter,system-ui,sans-serif;color:#17212b;background:#f3f6f7}
*{box-sizing:border-box}body{margin:0}main{max-width:1400px;margin:auto;padding:30px 22px 60px}
h1{margin:0 0 8px;font-size:30px}h2{font-size:20px;margin:0 0 10px}p{line-height:1.5}
.intro,.muted{color:#566670}.card{background:#fff;border:1px solid #d8e2e5;border-radius:14px;padding:20px;margin:18px 0}
.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}.stat{background:#eaf2f3;padding:14px;border-radius:10px}.stat b{display:block;font-size:24px;margin-top:4px}
.scroll{overflow:auto;max-height:620px;border:1px solid #e1e8ea}table{border-collapse:collapse;width:100%;font-size:12px;background:white}
th{position:sticky;top:0;background:#e9f0f2;z-index:1}th,td{padding:8px 9px;border-bottom:1px solid #e4eaec;text-align:right;white-space:nowrap}th:first-child,td:first-child{text-align:left}
.bars{display:grid;gap:9px}.bar-row{display:grid;grid-template-columns:75px 1fr 100px;gap:10px;align-items:center;font-size:13px}.track{height:15px;background:#e6ecee;border-radius:8px;overflow:hidden}.fill{height:100%;background:#187a72}
.warning{background:#fff3dc;border-left:4px solid #b27b20;padding:13px 15px;border-radius:7px}.good{color:#12634e}.bad{color:#9a4141}
@media(max-width:800px){.stats{grid-template-columns:1fr 1fr}.bar-row{grid-template-columns:55px 1fr 80px}main{padding:20px 12px}}
"""


def fmt(value: object, digits: int = 2) -> str:
    if value is None:
        return "—"
    if isinstance(value, (int, float)):
        if abs(value) >= 1_000_000:
            return f"{value / 1_000_000:.2f}M"
        if abs(value) >= 1000:
            return f"{value:,.0f}"
        return f"{value:.{digits}f}" if isinstance(value, float) else f"{value:,}"
    return str(value)


def table(headers: list[str], rows: list[list[object]]) -> str:
    head = "".join(f"<th>{html.escape(header)}</th>" for header in headers)
    body = "".join(
        "<tr>" + "".join(f"<td>{html.escape(fmt(value))}</td>" for value in row) + "</tr>"
        for row in rows
    )
    return f'<div class="scroll"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


def bars(rows: list[tuple[object, float]], title: str) -> str:
    maximum = max((value for _, value in rows), default=1) or 1
    body = []
    for label, value in rows:
        width = 100 * value / maximum
        body.append(
            f'<div class="bar-row"><span>{html.escape(str(label))}</span>'
            f'<div class="track"><div class="fill" style="width:{width:.2f}%"></div></div>'
            f'<strong>{html.escape(fmt(value, 0))}</strong></div>'
        )
    return f'<section class="card"><h2>{html.escape(title)}</h2><div class="bars">{"".join(body)}</div></section>'


def rows(connection: sqlite3.Connection, query: str) -> list[list[object]]:
    return [list(row) for row in connection.execute(query)]


def build(database: Path, output: Path) -> None:
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    scenarios = connection.execute("SELECT COUNT(*) FROM scenarios").fetchone()[0]
    completed = connection.execute(
        "SELECT COUNT(*) FROM scenarios WHERE status='complete'"
    ).fetchone()[0]
    states = connection.execute("SELECT COUNT(*) FROM states").fetchone()[0]
    transitions = connection.execute("SELECT COUNT(*) FROM transitions").fetchone()[0]

    per_day = rows(connection, """
        SELECT day, SUM(source_states), SUM(explored_trajectories),
               SUM(transition_rows), SUM(unique_candidates), SUM(retained_states)
        FROM layers WHERE status='complete' GROUP BY day ORDER BY day
    """)
    scenario_rows = rows(connection, """
        SELECT roommates, capacity, ROUND(actual_socks_per_person,2),
               CASE WHEN budget_per_person IS NULL THEN 'unlimited'
                    ELSE printf('$%g',budget_per_person) END, seed, status,
               (SELECT COUNT(*) FROM layers l WHERE l.scenario_id=s.id AND l.status='complete'),
               (SELECT COALESCE(SUM(transition_rows),0) FROM layers l WHERE l.scenario_id=s.id)
        FROM scenarios s ORDER BY roommates,capacity,budget_per_person,seed
    """)
    condition_rows = rows(connection, """
        SELECT s.roommates, ROUND(s.actual_socks_per_person,2),
               CASE WHEN s.budget_per_person IS NULL THEN 'unlimited'
                    ELSE printf('$%g',s.budget_per_person) END,
               COUNT(*), ROUND(AVG(t.main_immediate),2),
               ROUND(AVG(t.others_immediate_mean),2),
               ROUND(AVG(t.next_drawer_size*1.0/s.roommates),2),
               ROUND(AVG(t.main_discard_count),3),
               ROUND(AVG(t.packs_bought),3),
               ROUND(100.0*AVG(t.main_immediate<t.others_immediate_mean),2)
        FROM transitions t JOIN scenarios s ON s.id=t.scenario_id
        GROUP BY s.roommates,s.capacity,s.budget
        ORDER BY s.roommates,s.capacity,s.budget
    """)
    action_rows = rows(connection, """
        SELECT main_discard_count, COUNT(*), ROUND(AVG(main_immediate),2),
               ROUND(AVG(others_immediate_mean),2),
               ROUND(AVG(others_immediate_mean-main_immediate),2),
               ROUND(AVG(next_drawer_size),2), ROUND(AVG(spent_today),3),
               ROUND(AVG(main_holes),4),
               ROUND(100.0*AVG(main_immediate<others_immediate_mean),2)
        FROM transitions GROUP BY main_discard_count ORDER BY main_discard_count
    """)
    stock_rows = rows(connection, """
        SELECT CASE
                 WHEN 1.0*p.drawer_size/s.roommates<2 THEN '<2'
                 WHEN 1.0*p.drawer_size/s.roommates<4 THEN '2-<4'
                 WHEN 1.0*p.drawer_size/s.roommates<6 THEN '4-<6'
                 WHEN 1.0*p.drawer_size/s.roommates<10 THEN '6-<10'
                 ELSE '10+'
               END AS stock_per_person,
               t.main_discard_count, COUNT(*),
               ROUND(AVG(t.main_immediate),2),
               ROUND(AVG(t.others_immediate_mean),2),
               ROUND(AVG(t.next_drawer_size-p.drawer_size),3),
               ROUND(AVG(t.next_total_sockless-p.total_sockless),4)
        FROM transitions t
        JOIN states p ON p.id=t.parent_state_id
        JOIN scenarios s ON s.id=t.scenario_id
        GROUP BY stock_per_person,t.main_discard_count
        ORDER BY stock_per_person,t.main_discard_count
    """)
    coverage_rows = rows(connection, """
        SELECT day, SUM(chance_realizations), SUM(explored_trajectories),
               SUM(transition_rows), SUM(unique_candidates), SUM(retained_states),
               ROUND(100.0*SUM(retained_states)/NULLIF(SUM(unique_candidates),0),3)
        FROM layers WHERE status='complete' GROUP BY day ORDER BY day
    """)

    cards = "".join(
        f'<div class="stat"><span>{label}</span><b>{fmt(value)}</b></div>'
        for label, value in (
            ("Scenarios complete", f"{completed}/{scenarios}"),
            ("Retained states", states),
            ("Recorded transitions", transitions),
            ("Database size", f"{database.stat().st_size / 1024**3:.2f} GiB"),
        )
    )
    day_bars = [(row[0], float(row[3])) for row in per_day]
    page = f"""<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
    <title>Sock decision-tree report</title><style>{STYLE}</style><main>
    <h1>Bounded sequential multi-roommate decision tree</h1>
    <p class="intro">Each retained state branches through sampled random orders. Roommates draw and act one at a time; kept leftovers return immediately, so earlier choices change later hands. Every baseline-context individual action is covered, and additional trajectories sample sequential two-roommate interactions. Equivalent next-day states merge before a stratified frontier is retained.</p>
    <div class="warning"><strong>Interpretation:</strong> transitions are coverage experiments, not outcomes under a probability distribution over player choices. Do not treat an unweighted average over trajectories as a tournament forecast. Use matched conditions and trajectory features to study action effects.</div>
    <div class="stats">{cards}</div>
    {bars(day_bars, "Recorded transition rows by simulated day")}
    <section class="card"><h2>Layer coverage and pruning</h2>{table(["Day","Chance realizations","Trajectories explored","Transitions","Unique next states","Retained states","Retained %"],coverage_rows)}</section>
    <section class="card"><h2>Parameter conditions</h2>{table(["Roommates","Initial socks/person","Budget/person","Transitions","Main embarrassment","Other mean embarrassment","Next socks/person","Main discards","Packs bought","Main below others %"],condition_rows)}</section>
    <section class="card"><h2>Main-player action effects</h2>{table(["Main discards","Transitions","Main embarrassment","Other mean","Other-main margin","Next drawer","Spend today","Main holes","Main below others %"],action_rows)}</section>
    <section class="card"><h2>Effects under stock pressure</h2>{table(["Parent drawer socks/person","Main discards","Transitions","Main embarrassment","Other mean","Drawer change","New sockless"],stock_rows)}</section>
    <section class="card"><h2>Scenario progress</h2>{table(["n","Capacity","Socks/person","Budget/person","Seed","Status","Layers complete","Transitions"],scenario_rows)}</section>
    </main></html>"""
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(page, encoding="utf-8")
    connection.close()
    print(output.resolve())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "database", nargs="?", type=Path,
        default=Path("datasets/sock_tree_sequential.sqlite"),
    )
    parser.add_argument("--output", type=Path, default=Path("results/tree_report.html"))
    args = parser.parse_args()
    build(args.database, args.output)


if __name__ == "__main__":
    main()
