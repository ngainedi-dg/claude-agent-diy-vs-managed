import pandas as pd
import pytest

from task.generate_data import generate, to_csv_bytes
from tests.conftest import ROOT


@pytest.fixture(scope="module")
def data():
    df, truth = generate(42)
    return df, truth


def entry(truth, aid):
    return next(e for e in truth["anomalies"] if e["id"] == aid)


def recomputed(df):
    return (df.quantity * df.unit_price * (1 - df.discount_pct / 100)).round(2)


def test_deterministic_for_a_seed(data):
    df, truth = data
    df2, truth2 = generate(42)
    assert to_csv_bytes(df) == to_csv_bytes(df2)
    assert truth == truth2


def test_different_seed_gives_different_data(data):
    df, _ = data
    other, _ = generate(7)
    assert to_csv_bytes(df) != to_csv_bytes(other)


def test_shape_and_range(data):
    df, _ = data
    assert list(df.columns) == ["transaction_id", "date", "customer_id", "product_id", "category",
                                "region", "quantity", "unit_price", "discount_pct", "revenue"]
    assert 9500 <= len(df) <= 10500
    assert df.date.min() >= "2025-01-01" and df.date.max() <= "2025-12-31"


def test_ground_truth_entries(data):
    _, truth = data
    entries = truth["anomalies"]
    assert len({e["id"] for e in entries}) == len(entries)
    assert {e["kind"] for e in entries} <= {"injected_defect", "injected_anomaly", "ambiguous", "legitimate_pattern"}
    injected = [e for e in entries if e["kind"].startswith("injected")]
    assert 10 <= len(injected) + 1 <= 15  # plus the ambiguous one
    assert any(e["kind"] == "legitimate_pattern" for e in entries)
    assert all(e["match"] for e in entries)


def test_row_level_defects_are_in_the_data(data):
    df, truth = data
    by_id = df.drop_duplicates("transaction_id").set_index("transaction_id")

    a04 = by_id.loc[entry(truth, "A04")["transaction_ids"]]
    ratio = a04.revenue / recomputed(a04)
    assert ((ratio - 1).abs() > 0.3).all()

    a05 = by_id.loc[entry(truth, "A05")["transaction_ids"]]
    assert (a05.quantity >= 400).all()
    assert ((a05.revenue - recomputed(a05)).abs() < 0.02).all()

    assert by_id.loc[entry(truth, "A06")["transaction_ids"]].category.isna().all()

    a07 = by_id.loc[entry(truth, "A07")["transaction_ids"]]
    canonical = {"Electronics", "Furniture", "Office Supplies", "Apparel", "Home & Garden"}
    assert (~a07.category.isin(canonical)).sum() >= 15 or a07.category.nunique() > 1

    counts = df.transaction_id.value_counts()
    assert (counts[entry(truth, "A10")["transaction_ids"]] == 2).all()
    assert df.duplicated().sum() == 15

    a11 = by_id.loc[entry(truth, "A11")["transaction_ids"]]
    assert (a11.quantity < 0).all() and (a11.revenue < 0).all()


def test_business_anomalies_show_up_in_the_data(data):
    df, truth = data
    df = df.assign(dt=pd.to_datetime(df.date))

    west = df.region == "West"
    window = df.dt.between("2025-08-01", "2025-10-15")
    share_in, share_out = west[window].mean(), west[~window].mean()
    assert share_in < 0.8 * share_out  # A01

    apparel = df.category == "Apparel"
    window = df.dt.between("2025-02-10", "2025-03-09")
    assert apparel[window].mean() < 0.7 * apparel[~window].mean()  # A09

    hot = df[df.product_id.isin(["P003", "P012"]) & df.dt.between("2025-03-15", "2025-04-14")]
    assert len(hot) > 10 and hot.discount_pct.mean() >= 40  # A02

    daily = df.groupby("date").size()
    assert daily["2025-06-15"] >= 4 * daily.median()  # A03

    oct_rows = df[(df.customer_id == "C0777") & (df.date.str[:7] == "2025-10")]
    assert len(oct_rows) >= 100  # A08

    assert df[df.product_id == "P041"].date.min() >= "2025-09-01"  # L03
    bf = df.dt.between("2025-11-24", "2025-11-30")
    assert df[bf].discount_pct.mean() > df[~bf].discount_pct.mean() + 5  # L01


def test_ground_truth_is_kept_away_from_the_agents(data):
    _, truth = data
    task_files = {p.name for p in (ROOT / "task").rglob("*") if p.is_file()}
    assert not any("truth" in n.lower() for n in task_files)

    prompt = (ROOT / "task" / "instructions.md").read_text(encoding="utf-8")
    for leak in ("C0777", "P003", "P012", "P020", "P041", "Black Friday", "ground_truth",
                 "West", "Apparel", "2025-08-01", "2025-10-15"):
        assert leak not in prompt
