"""Reproduce the retail analysis: python src/analyze.py [--download]."""
from pathlib import Path
import argparse
import hashlib
import json
import urllib.request
import zipfile
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter

ROOT = Path(__file__).resolve().parents[1]
URL = 'https://archive.ics.uci.edu/static/public/352/online+retail.zip'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--download', action='store_true')
    args = parser.parse_args()
    raw = ROOT / 'data/raw'
    raw.mkdir(parents=True, exist_ok=True)
    archive = raw / 'online-retail.zip'
    if args.download and not archive.exists():
        urllib.request.urlretrieve(URL, archive)
    path = raw / 'Online Retail.xlsx'
    if not path.exists():
        if not archive.exists():
            raise SystemExit('Run python src/analyze.py --download first.')
        with zipfile.ZipFile(archive) as z:
            path.write_bytes(z.read('Online Retail.xlsx'))
    df = pd.read_excel(path, dtype={'InvoiceNo': str, 'StockCode': str, 'CustomerID': str})
    expected = ['InvoiceNo','StockCode','Description','Quantity','InvoiceDate','UnitPrice','CustomerID','Country']
    assert list(df.columns) == expected, 'Unexpected source schema'
    df['InvoiceDate'] = pd.to_datetime(df['InvoiceDate'], errors='raise')
    # Sequential exclusions reconcile exactly to the retained purchase rows.
    work = df.copy()
    audit = [{'step': 'Original product lines', 'removed': 0, 'remaining': len(work)}]
    def exclude(label, mask):
        nonlocal work
        removed = int(mask.sum())
        work = work.loc[~mask].copy()
        audit.append({'step': label, 'removed': removed, 'remaining': len(work)})
    exclude('Exact repeated lines removed (assumption)', work.duplicated())
    exclude('Cancellation invoices excluded', work.InvoiceNo.str.upper().str.startswith('C', na=False))
    exclude('Non-positive quantity excluded', work.Quantity.le(0))
    exclude('Non-positive unit price excluded', work.UnitPrice.le(0))
    # Integer millionths of a pound avoid floating-point accumulation in currency totals.
    microgbp = work.UnitPrice.mul(1_000_000)
    assert (microgbp.sub(microgbp.round()).abs() < 1e-6).all(), 'Price precision exceeds one millionth of a pound'
    work['sales_microgbp'] = work.Quantity.astype('int64') * microgbp.round().astype('int64')
    work['sales_gbp'] = work.sales_microgbp / 1_000_000
    identified = work.dropna(subset=['CustomerID']).copy()
    assert len(work) + sum(a['removed'] for a in audit) == len(df)
    orders = work.groupby('InvoiceNo').sales_microgbp.sum()
    # Use one day after the final source date, rather than today's date.
    snapshot = df.InvoiceDate.max().normalize() + pd.Timedelta(days=1)
    customers = identified.groupby('CustomerID').agg(
        last_purchase=('InvoiceDate','max'), frequency=('InvoiceNo','nunique'),
        monetary_microgbp=('sales_microgbp','sum'))
    customers['recency_days'] = (snapshot - customers.last_purchase.dt.normalize()).dt.days
    customers['monetary_gbp'] = customers.monetary_microgbp / 1_000_000
    # Three explainable business groups; these are rules, not learned clusters.
    spend_threshold = float(customers.monetary_gbp.quantile(.75))
    customers['segment'] = 'Occasional or inactive'
    active = customers.recency_days.le(90)
    customers.loc[active & customers.frequency.ge(2), 'segment'] = 'Active repeat'
    customers.loc[active & customers.frequency.ge(2) & customers.monetary_gbp.ge(spend_threshold), 'segment'] = 'High-value repeat'
    repeat = float(customers.frequency.ge(2).mean())
    metrics = {
        'gross_purchase_sales_gbp': int(work.sales_microgbp.sum()) / 1_000_000,
        'purchase_orders': len(orders),
        'identified_purchasing_customers': len(customers),
        'average_purchase_order_value_gbp': int(work.sales_microgbp.sum()) / 1_000_000 / len(orders),
        'identified_repeat_customer_rate': repeat,
    }
    segments = customers.groupby('segment').agg(customers=('frequency','size'),
        sales_microgbp=('monetary_microgbp','sum'), median_recency_days=('recency_days','median'),
        median_orders=('frequency','median'), median_spend_gbp=('monetary_gbp','median'))
    segments['sales_gbp'] = segments.sales_microgbp / 1_000_000
    segments['sales_share'] = segments.sales_microgbp / segments.sales_microgbp.sum()
    countries = work.groupby('Country').agg(sales_microgbp=('sales_microgbp','sum'), orders=('InvoiceNo','nunique')).sort_values('sales_microgbp',ascending=False)
    countries['sales_gbp'] = countries.sales_microgbp / 1_000_000
    monthly = work.groupby(work.InvoiceDate.dt.to_period('M')).sales_microgbp.sum().to_frame()
    monthly['sales_gbp'] = monthly.sales_microgbp / 1_000_000
    assert int(orders.sum()) == int(countries.sales_microgbp.sum()) == int(monthly.sales_microgbp.sum()) == int(work.sales_microgbp.sum())
    assert int(segments.customers.sum()) == len(customers)
    assert int(segments.sales_microgbp.sum()) == int(identified.sales_microgbp.sum())
    # A sensitivity result makes the duplicate assumption visible.
    no_dedup = df.loc[~df.InvoiceNo.str.upper().str.startswith('C',na=False) & df.Quantity.gt(0) & df.UnitPrice.gt(0)]
    meta = {
        'source_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
        'source_rows':len(df), 'purchase_rows':len(work),
        'missing_customer_id_source_rows':int(df.CustomerID.isna().sum()),
        'missing_customer_id_purchase_rows':int(work.CustomerID.isna().sum()),
        'identified_purchase_sales_gbp':int(identified.sales_microgbp.sum()) / 1_000_000,
        'purchase_sales_without_deduplication_gbp':round(float((no_dedup.Quantity * no_dedup.UnitPrice).sum()),2),
        'first_timestamp':str(df.InvoiceDate.min()), 'last_timestamp':str(df.InvoiceDate.max()),
        'segmentation_snapshot':str(snapshot.date()), 'active_window_days':90,
        'high_value_spend_threshold_gbp':spend_threshold,
        'checks':'Row exclusions, country/month/order sales, segment counts and sales reconciled.'
    }
    out = ROOT / 'results'
    out.mkdir(exist_ok=True)
    for filename, data in [('kpis',metrics), ('data_quality',meta)]:
        (out / f'{filename}.json').write_text(json.dumps(data,indent=2)+'\n')
    pd.DataFrame(audit).to_csv(out/'cleaning_audit.csv',index=False)
    segments.to_csv(out/'segment_summary.csv')
    countries.to_csv(out/'country_sales.csv')
    monthly.to_csv(out/'monthly_sales.csv')
    # Customer-level table stays local, excluded from publication below.
    customers.to_csv(raw/'customer_segments.csv')
    plt.rcParams.update({'font.family':'DejaVu Sans','font.size':11,'axes.spines.top':False,'axes.spines.right':False,'figure.facecolor':'#fafafa','axes.facecolor':'#fafafa'})
    charts = ROOT/'charts'
    charts.mkdir(exist_ok=True)
    def save(name):
        plt.tight_layout()
        plt.savefig(charts/name,dpi=160,bbox_inches='tight')
        plt.savefig(charts/Path(name).with_suffix('.svg'),bbox_inches='tight',metadata={'Date':None})
        plt.close()
    fig, ax = plt.subplots(figsize=(10,5))
    ax.plot(monthly.index.astype(str),monthly.sales_gbp,marker='o',color='#167d9a')
    ax.tick_params(axis='x',rotation=45)
    ax.yaxis.set_major_formatter(FuncFormatter(lambda x,_:f'£{x/1000:,.0f}k'))
    ax.set(title='Monthly gross purchase sales',ylabel='GBP',xlabel='December 2011 ends on 9 December; it is a partial month.')
    save('monthly_sales.png')
    fig, axes = plt.subplots(1,2,figsize=(12,5))
    segments.customers.plot.barh(ax=axes[0],color='#167d9a')
    (segments.sales_share*100).plot.barh(ax=axes[1],color='#d48b38')
    axes[0].set(title='Identified customers',ylabel='',xlabel='Customers')
    axes[1].set(title='Share of identified-customer purchase sales',ylabel='',xlabel='Percent')
    save('customer_segments.png')
    fig,ax = plt.subplots(figsize=(10,5))
    countries.head(10).sort_values('sales_gbp').sales_gbp.plot.barh(ax=ax,color='#167d9a')
    ax.xaxis.set_major_formatter(FuncFormatter(lambda x,_:f'£{x/1e6:,.1f}m'))
    ax.set(title='Top 10 countries by gross purchase sales',ylabel='',xlabel='GBP · purchases including missing customer IDs')
    save('country_sales.png')
    print(json.dumps({'kpis':metrics,'quality':meta,'segments':segments.reset_index().to_dict('records')},indent=2))


if __name__ == '__main__':
    main()
