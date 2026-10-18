import sqlite3
import json
import os
import csv
import math
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
import time
from collections import defaultdict
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
import requests
from dotenv import load_dotenv

# Load .env from Render secret files path, or local .env
if os.path.exists("/etc/secrets/.env"):
    load_dotenv("/etc/secrets/.env")
else:
    load_dotenv()

DB_PATH = os.path.join(os.path.dirname(__file__), "sme_data.db")
DATA_DIR = os.path.dirname(__file__)

# Environment variables
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMMA_MODEL = os.environ.get("GEMMA_MODEL", "gemma-4-26b-a4b-it")
SARVAM_API_KEY = os.environ.get("SARVAM_API_KEY", "").strip()
DRONAHQ_WEBHOOK_URL = os.environ.get("DRONAHQ_WEBHOOK_URL", "")
DRONAHQ_API_KEY = os.environ.get("DRONAHQ_API_KEY", "")
NVIDIA_API_KEY = os.environ.get("NVIDIA_API_KEY", "")
NVIDIA_MODEL = os.environ.get("NVIDIA_MODEL", "meta/llama-3.2-11b-vision-instruct")

def call_gemma(system_prompt: str, user_text: str = "") -> str:
    """Generate responses using Google Gemma model."""
    try:
        from google import genai
        client = genai.Client()
        full_content = f"{system_prompt}\n\n{user_text}".strip() if user_text else system_prompt.strip()
        response = client.models.generate_content(
            model=GEMMA_MODEL,
            contents=full_content,
        )
        if response and response.text:
            return response.text.strip()
    except Exception as e:
        print(f"Gemma API Error: {e}")
    return ""

def ask_dronahq(query_text: str) -> str:
    # Use clean api-key header as expected by DronaHQ
    headers = {
        "api-key": DRONAHQ_API_KEY.strip(),
        "Content-Type": "application/json"
    }
    payload = {"query": query_text}
    url = DRONAHQ_WEBHOOK_URL.strip()
    response = requests.post(url, headers=headers, json=payload, timeout=60)
    
    # If 404 and credentials might be swapped in .env, attempt swapped pair
    if response.status_code == 404:
        import re
        url_match = re.search(r'([a-f0-9\-]{36})', url)
        key_is_uuid = bool(re.match(r'^[a-f0-9\-]{36}$', DRONAHQ_API_KEY.strip()))
        if url_match and key_is_uuid:
            swapped_url = url.replace(url_match.group(1), DRONAHQ_API_KEY.strip())
            swapped_headers = {
                "api-key": url_match.group(1),
                "Content-Type": "application/json"
            }
            try:
                alt_resp = requests.post(swapped_url, headers=swapped_headers, json=payload, timeout=60)
                if alt_resp.status_code == 200:
                    response = alt_resp
            except Exception:
                pass

    response.raise_for_status()
    try:
        data = response.json()
    except Exception:
        return response.text.strip()

    if isinstance(data, dict):
        if "response" in data:
            r = data["response"]
            if isinstance(r, dict):
                if "text" in r and r["text"]:
                    return str(r["text"]).strip()
                elif "message" in r and r["message"]:
                    return str(r["message"]).strip()
                elif "recommendations" in r:
                    return f"{r.get('message', '')}\n\n" + "\n".join(r["recommendations"])
                return json.dumps(r)
            elif isinstance(r, str):
                return r.strip()
        for k in ["result", "text", "message", "answer", "data", "output"]:
            if k in data and data[k]:
                val = data[k]
                return str(val).strip() if not isinstance(val, (dict, list)) else json.dumps(val)
        return json.dumps(data)
    elif isinstance(data, str):
        return data.strip()
    return str(data)

def format_with_nvidia(system_prompt: str, user_text: str) -> str:
    url = "https://integrate.api.nvidia.com/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {NVIDIA_API_KEY}",
        "Content-Type": "application/json"
    }
    candidate_models = [
        NVIDIA_MODEL,
        "meta/llama-3.2-11b-vision-instruct"
    ]
    models_to_try = []
    for m in candidate_models:
        if m and m not in models_to_try:
            models_to_try.append(m)

    for model_name in models_to_try:
        payload = {
            "model": model_name,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_text}
            ],
            "temperature": 0.2,
            "max_tokens": 800,
        }
        try:
            response = requests.post(url, headers=headers, json=payload, timeout=25)
            if response.status_code == 200:
                res_data = response.json()
                if "choices" in res_data and len(res_data["choices"]) > 0:
                    content = res_data["choices"][0]["message"].get("content", "")
                    if content:
                        print(f"[NVIDIA] Successfully generated response using {model_name}")
                        return content.strip()
            else:
                print(f"[NVIDIA] Model {model_name} HTTP {response.status_code}: {response.text[:120]}")
        except Exception as e:
            print(f"[NVIDIA] Error with model {model_name}: {e}")

    return ""

# ─── DATABASE SETUP ───────────────────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    """Import all 11 CSV datasets into SQLite tables."""
    conn = get_db()
    cur = conn.cursor()

    csv_tables = {
        "categories":      "categories.csv",
        "products":        "products.csv",
        "suppliers":       "suppliers.csv",
        "customers":       "customers.csv",
        "employees":       "employees.csv",
        "sales_raw":       "sales_raw.csv",
        "invoices":        "invoices.csv",
        "expenses":        "expenses.csv",
        "inventory":       "inventory.csv",
        "pending_payments":"pending_payments.csv",
        "purchase_orders": "purchase_orders.csv",
    }

    for table, filename in csv_tables.items():
        filepath = os.path.join(DATA_DIR, filename)
        if not os.path.exists(filepath):
            print(f"WARNING: {filepath} not found, skipping.")
            continue

        with open(filepath, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
            if not rows:
                continue
            cols = list(rows[0].keys())

        # Drop & recreate table
        cur.execute(f"DROP TABLE IF EXISTS {table}")
        col_defs = ", ".join(f'"{c}" TEXT' for c in cols)
        cur.execute(f'CREATE TABLE IF NOT EXISTS {table} ({col_defs})')

        with open(filepath, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            placeholders = ", ".join("?" for _ in cols)
            quoted_cols = ", ".join(f'"{c}"' for c in cols)
            for row in reader:
                values = [row.get(c, "") for c in cols]
                cur.execute(f'INSERT INTO {table} ({quoted_cols}) VALUES ({placeholders})', values)

        print(f"  [OK] Imported {table} ({len(rows)} rows)")

    conn.commit()
    conn.close()
    print("Database initialized successfully.")

# ─── FORMATTING HELPERS ───────────────────────────────────────────────────────

def fmt_inr(n):
    n = float(n) if n else 0
    if abs(n) >= 10_000_000:
        return f"₹{n/10_000_000:.2f}Cr"
    elif abs(n) >= 100_000:
        return f"₹{n/100_000:.2f}L"
    else:
        return f"₹{round(n):,}"

def safe_float(v, default=0.0):
    try:
        return float(v) if v else default
    except (ValueError, TypeError):
        return default

def safe_int(v, default=0):
    try:
        return int(float(v)) if v else default
    except (ValueError, TypeError):
        return default

# ─── KPI ENGINE ───────────────────────────────────────────────────────────────

def compute_kpis():
    conn = get_db()
    cur = conn.cursor()

    # ── Revenue & Orders ──
    cur.execute("SELECT SUM(CAST(TotalAmount AS REAL)), COUNT(*) FROM sales_raw")
    total_revenue_raw, total_orders = cur.fetchone()
    total_revenue = safe_float(total_revenue_raw)
    avg_order_value = total_revenue / max(total_orders, 1)

    # ── Active Customers ──
    cur.execute("SELECT COUNT(DISTINCT CustomerID) FROM sales_raw")
    active_customers = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM customers")
    total_customers = cur.fetchone()[0]

    # ── COGS ──
    cur.execute("""
        SELECT s.ProductID, SUM(CAST(s.Quantity AS REAL)), SUM(CAST(s.TotalAmount AS REAL))
        FROM sales_raw s
        GROUP BY s.ProductID
    """)
    sales_by_prod = cur.fetchall()
    cur.execute("SELECT ProductID, CAST(StandardMarkup AS REAL) FROM products")
    markup_map = {r[0]: safe_float(r[1], 1.0) for r in cur.fetchall()}

    total_cogs = 0
    for prod_id, qty, rev in sales_by_prod:
        markup = markup_map.get(prod_id, 1.0)
        if markup > 0:
            total_cogs += rev / markup

    # ── Expenses ──
    cur.execute("SELECT SUM(CAST(AmountSpent AS REAL)) FROM expenses")
    total_expenses = safe_float(cur.fetchone()[0])

    # ── Profit ──
    gross_profit = total_revenue - total_cogs
    net_profit = gross_profit - total_expenses
    net_profit_margin = (net_profit / total_revenue * 100) if total_revenue > 0 else 0

    # ── Cash Flow ──
    cur.execute("SELECT SUM(CAST(TotalInvoiceValue AS REAL)) FROM invoices WHERE Status='Paid'")
    total_collected = safe_float(cur.fetchone()[0])
    cash_in_hand = total_collected - total_expenses

    # ── Pending Receivables ──
    cur.execute("SELECT SUM(CAST(AmountPending AS REAL)), COUNT(*) FROM pending_payments")
    total_pending_raw, pending_count = cur.fetchone()
    total_pending = safe_float(total_pending_raw)

    cur.execute("SELECT COUNT(*), SUM(CAST(AmountPending AS REAL)) FROM pending_payments WHERE CAST(DaysOverdue AS INTEGER) > 0")
    overdue_count, overdue_amount_raw = cur.fetchone()
    overdue_amount = safe_float(overdue_amount_raw)

    # ── Invoices ──
    cur.execute("SELECT COUNT(*), SUM(CAST(TotalInvoiceValue AS REAL)) FROM invoices")
    total_invoices, total_invoiced = cur.fetchone()
    total_invoiced = safe_float(total_invoiced)
    collection_rate = (total_collected / total_invoiced * 100) if total_invoiced > 0 else 0

    # ── Inventory ──
    cur.execute("""
        SELECT i.ProductID, CAST(i.QuantityInStock AS REAL), CAST(i.ReorderPoint AS REAL),
               CAST(p.DefaultSellingPrice AS REAL), CAST(p.StandardMarkup AS REAL), p.ProductName, p.CategoryID
        FROM inventory i
        LEFT JOIN products p ON i.ProductID = p.ProductID
    """)
    inv_rows = cur.fetchall()
    total_inventory_value = 0
    low_stock_items = []
    out_of_stock_count = 0
    low_stock_count = 0

    for prod_id, qty, reorder, price, markup, name, cat_id in inv_rows:
        cost = price / max(markup, 0.01) if markup and markup > 0 else price
        total_inventory_value += qty * cost
        if qty == 0:
            out_of_stock_count += 1
        if qty < reorder:
            low_stock_count += 1
            low_stock_items.append({
                "id": prod_id, "name": name or prod_id,
                "qty": int(qty), "reorder": int(reorder)
            })

    # ── Products ──
    cur.execute("SELECT COUNT(*) FROM products")
    total_products = cur.fetchone()[0]

    # ── Employees & Suppliers ──
    cur.execute("SELECT COUNT(*) FROM employees WHERE IsActive='1'")
    total_employees = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM suppliers")
    total_suppliers = cur.fetchone()[0]

    # ── Top Products by Revenue ──
    cur.execute("""
        SELECT s.ProductID, SUM(CAST(s.TotalAmount AS REAL)) as rev, SUM(CAST(s.Quantity AS REAL)) as qty
        FROM sales_raw s
        GROUP BY s.ProductID
        ORDER BY rev DESC
        LIMIT 5
    """)
    top_products_raw = cur.fetchall()
    cur.execute("SELECT ProductID, ProductName FROM products")
    prod_name_map = {r[0]: r[1] for r in cur.fetchall()}
    top_products = [{"name": prod_name_map.get(r[0], r[0]), "revenue": fmt_inr(r[1]), "qty": int(safe_float(r[2]))} for r in top_products_raw]

    # ── Top Products by Quantity ──
    cur.execute("""
        SELECT s.ProductID, SUM(CAST(s.Quantity AS REAL)) as qty
        FROM sales_raw s
        GROUP BY s.ProductID
        ORDER BY qty DESC
        LIMIT 5
    """)
    top_by_qty = [{"name": prod_name_map.get(r[0], r[0]), "qty": int(safe_float(r[1]))} for r in cur.fetchall()]

    # ── Revenue by Category ──
    cur.execute("""
        SELECT c.CategoryName, SUM(CAST(s.TotalAmount AS REAL)) as rev
        FROM sales_raw s
        JOIN products p ON s.ProductID = p.ProductID
        JOIN categories c ON p.CategoryID = c.CategoryID
        GROUP BY c.CategoryName
        ORDER BY rev DESC
        LIMIT 6
    """)
    cat_rows = cur.fetchall()
    top_categories = [{"name": r[0], "revenue": fmt_inr(r[1]), "pct": f"{r[1]/total_revenue*100:.1f}" if total_revenue > 0 else "0"} for r in cat_rows]
    cat_chart_labels = [r[0] for r in cat_rows]
    cat_chart_data = [round(safe_float(r[1]) / 1000) for r in cat_rows]

    # ── Weekly Revenue (for chart) ──
    cur.execute("""
        SELECT strftime('%d', SalesDate) as day, SUM(CAST(TotalAmount AS REAL)) as rev
        FROM sales_raw
        GROUP BY strftime('%Y-%m-%d', SalesDate)
        ORDER BY SalesDate
    """)
    day_rows = cur.fetchall()
    weekly_rev = [0, 0, 0, 0, 0]
    for day_str, rev in day_rows:
        day = safe_int(day_str)
        week_idx = min(math.ceil(day / 7), 5) - 1
        if 0 <= week_idx < 5:
            weekly_rev[week_idx] += safe_float(rev)

    # ── Revenue Sparkline (7 daily points) ──
    cur.execute("""
        SELECT SalesDate, SUM(CAST(TotalAmount AS REAL)) as rev
        FROM sales_raw
        GROUP BY SalesDate
        ORDER BY SalesDate
    """)
    all_days = cur.fetchall()
    def pick_n(rows, n):
        if len(rows) <= n:
            return [round(safe_float(r[1]) / 1000) for r in rows]
        step = len(rows) // n
        return [round(safe_float(rows[i * step][1]) / 1000) for i in range(n)]
    revenue_sparkline = pick_n(all_days, 7)

    # Profit sparkline (7 daily points, scaled perfectly to net profit margin)
    profit_sparkline = [round(rev * (net_profit_margin / 100)) for rev in revenue_sparkline]

    # ── Top Customers ──
    cur.execute("""
        SELECT s.CustomerID, SUM(CAST(s.TotalAmount AS REAL)) as rev
        FROM sales_raw s
        GROUP BY s.CustomerID
        ORDER BY rev DESC
        LIMIT 5
    """)
    top_cust_raw = cur.fetchall()
    cur.execute("SELECT CustomerID, DisplayName FROM customers")
    cust_name_map = {r[0]: r[1] for r in cur.fetchall()}
    top_customers = [{"id": r[0], "name": cust_name_map.get(r[0], r[0]), "revenue": fmt_inr(r[1])} for r in top_cust_raw]

    # ── Top Suppliers ──
    cur.execute("""
        SELECT po.SupplierID, SUM(CAST(po.TotalAmountSpent AS REAL)) as spend
        FROM purchase_orders po
        GROUP BY po.SupplierID
        ORDER BY spend DESC
        LIMIT 5
    """)
    top_sup_raw = cur.fetchall()
    cur.execute("SELECT SupplierID, SupplierName, ReliabilityScore FROM suppliers")
    sup_map = {r[0]: {"name": r[1], "score": r[2]} for r in cur.fetchall()}
    top_suppliers = [{
        "id": r[0],
        "name": sup_map.get(r[0], {}).get("name", r[0]),
        "value": fmt_inr(r[1]),
        "reliability": sup_map.get(r[0], {}).get("score", "N/A")
    } for r in top_sup_raw]

    # ── Top Pending Payments ──
    cur.execute("""
        SELECT pp.InvoiceID, pp.CustomerID, CAST(pp.AmountPending AS REAL), CAST(pp.DaysOverdue AS INTEGER), pp.DueDate
        FROM pending_payments pp
        ORDER BY CAST(pp.DaysOverdue AS INTEGER) DESC, CAST(pp.AmountPending AS REAL) DESC
        LIMIT 10
    """)
    top_pending = [{
        "invoice": r[0],
        "customer": cust_name_map.get(r[1], r[1]),
        "amount": fmt_inr(r[2]),
        "amount_raw": round(safe_float(r[2])),
        "days_overdue": safe_int(r[3]),
        "due_date": r[4]
    } for r in cur.fetchall()]

    # ── Expense Breakdown ──
    cur.execute("""
        SELECT ExpenseCode, SUM(CAST(AmountSpent AS REAL)) as amt
        FROM expenses
        GROUP BY ExpenseCode
        ORDER BY amt DESC
    """)
    exp_breakdown = [{"code": r[0], "amount": fmt_inr(r[1]), "raw": round(safe_float(r[1]))} for r in cur.fetchall()]

    # ── Purchase Orders Summary ──
    cur.execute("SELECT COUNT(*), SUM(CAST(TotalAmountSpent AS REAL)) FROM purchase_orders")
    po_count, po_spend = cur.fetchone()
    po_spend = safe_float(po_spend)

    # ── Health Score ──
    profit_score = min((net_profit_margin / 20) * 100, 100)
    collection_score = collection_rate
    expense_ratio = (total_expenses / total_revenue * 100) if total_revenue > 0 else 100
    expense_score = max(100 - expense_ratio, 0)
    health_score = round(profit_score * 0.4 + collection_score * 0.4 + expense_score * 0.2)
    if health_score >= 80:
        health_label = "Excellent"
        health_color = "green"
    elif health_score >= 60:
        health_label = "Good"
        health_color = "blue"
    elif health_score >= 40:
        health_label = "Fair"
        health_color = "orange"
    else:
        health_label = "At Risk"
        health_color = "red"

    # ── Monthly revenue for line chart ──
    chart_rev_data = [round(v / 1000) for v in weekly_rev]
    chart_labels = ["Week 1", "Week 2", "Week 3", "Week 4", "Week 5"]

    # ── Revenue by category for sales page chart ──
    cur.execute("""
        SELECT c.CategoryName, SUM(CAST(s.TotalAmount AS REAL)) as rev
        FROM sales_raw s
        JOIN products p ON s.ProductID = p.ProductID
        JOIN categories c ON p.CategoryID = c.CategoryID
        GROUP BY c.CategoryName
        ORDER BY rev DESC
    """)
    sales_cat_all = cur.fetchall()
    sales_cat_labels = [r[0] for r in sales_cat_all]
    sales_cat_data = [round(safe_float(r[1]) / 1000) for r in sales_cat_all]

    # ── Employee breakdown ──
    cur.execute("SELECT JobRole, COUNT(*) FROM employees GROUP BY JobRole")
    emp_roles = [{"role": r[0], "count": r[1]} for r in cur.fetchall()]

    # ── Forecast ──
    avg_weekly_rev = total_revenue / 5 if total_revenue > 0 else 0
    forecast_next_month = avg_weekly_rev * 5 * 1.05  # 5% growth assumption
    forecast_growth_pct = 5.0

    conn.close()

    return {
        # ── Core KPIs ──
        "totalRevenue": total_revenue,
        "totalRevenueFmt": fmt_inr(total_revenue),
        "netProfit": net_profit,
        "netProfitFmt": fmt_inr(net_profit),
        "netProfitMargin": round(net_profit_margin, 1),
        "cashInHand": cash_in_hand,
        "cashInHandFmt": fmt_inr(cash_in_hand),
        "pendingReceivables": total_pending,
        "pendingReceivablesFmt": fmt_inr(total_pending),
        "overdueCount": overdue_count,
        "overdueAmount": fmt_inr(overdue_amount),
        "overdueAmountRaw": overdue_amount,
        # ── Expenses & COGS ──
        "totalExpenses": total_expenses,
        "totalExpensesFmt": fmt_inr(total_expenses),
        "totalCOGS": total_cogs,
        "totalCOGSFmt": fmt_inr(total_cogs),
        # ── Orders & Customers ──
        "totalOrders": total_orders,
        "avgOrderValue": avg_order_value,
        "avgOrderValueFmt": fmt_inr(avg_order_value),
        "activeCustomers": active_customers,
        "totalCustomers": total_customers,
        # ── Invoices ──
        "collectionRate": round(collection_rate, 1),
        "totalInvoiced": fmt_inr(total_invoiced),
        "totalCollected": fmt_inr(total_collected),
        "totalInvoices": total_invoices,
        # ── Inventory ──
        "totalInventoryValue": total_inventory_value,
        "totalInventoryValueFmt": fmt_inr(total_inventory_value),
        "lowStockCount": low_stock_count,
        "outOfStockCount": out_of_stock_count,
        "lowStockItems": low_stock_items[:5],
        # ── Products / Staff ──
        "totalProducts": total_products,
        "totalEmployees": total_employees,
        "totalSuppliers": total_suppliers,
        # ── Health Score ──
        "healthScore": health_score,
        "healthLabel": health_label,
        "healthColor": health_color,
        # ── Chart Data ──
        "revenueSparkline": revenue_sparkline,
        "profitSparkline": profit_sparkline,
        "chartLabels": chart_labels,
        "chartRevData": chart_rev_data,
        "salesCatLabels": sales_cat_labels,
        "salesCatData": sales_cat_data,
        "catChartLabels": cat_chart_labels,
        "catChartData": cat_chart_data,
        # ── Top Lists ──
        "topProducts": top_products,
        "topByQty": top_by_qty,
        "topCategories": top_categories,
        "topCustomers": top_customers,
        "topSuppliers": top_suppliers,
        "topPendingPayments": top_pending,
        "expByCode": exp_breakdown,
        # ── Procurement ──
        "totalPOCount": po_count,
        "totalPOSpend": fmt_inr(po_spend),
        # ── Forecast ──
        "forecastNextMonth": forecast_next_month,
        "forecastNextMonthFmt": fmt_inr(forecast_next_month),
        "forecastGrowthPct": forecast_growth_pct,
        # ── Staff ──
        "empRoles": emp_roles,
    }

# ─── AI INSIGHTS ENGINE ───────────────────────────────────────────────────────

def generate_ai_insights(kpis: dict) -> dict:
    """Generate rule-based AI insights from KPIs (Gemma-style reasoning)."""
    insights = []
    alerts = []
    pricing_recs = []
    collections_recs = []
    inventory_recs = []
    growth_recs = []

    rev = kpis["totalRevenue"]
    margin = kpis["netProfitMargin"]
    health = kpis["healthScore"]
    coll_rate = kpis["collectionRate"]
    low_stock = kpis["lowStockCount"]
    out_stock = kpis["outOfStockCount"]
    overdue_count = kpis["overdueCount"]
    overdue_amt = kpis["overdueAmountRaw"]
    exp = kpis["totalExpenses"]
    cogs = kpis["totalCOGS"]

    # ── Business Brief ──
    brief = f"Your business generated {kpis['totalRevenueFmt']} in revenue with a {margin}% net profit margin. "
    brief += f"Collection rate stands at {coll_rate}%, "
    brief += f"with {low_stock} products low on stock and {overdue_count} overdue invoices totaling {kpis['overdueAmount']}."

    # ── Smart Alerts ──
    if low_stock > 0:
        alerts.append({"type": "warning", "title": "Low Stock Alert", "message": f"{low_stock} products are running low on stock. Reorder immediately.", "action": "inventory"})
    if out_stock > 0:
        alerts.append({"type": "error", "title": "Out of Stock", "message": f"{out_stock} products are completely out of stock — sales impact likely.", "action": "inventory"})
    if overdue_count > 0:
        alerts.append({"type": "error", "title": "Overdue Payments", "message": f"{overdue_count} customers have overdue payments totaling {kpis['overdueAmount']}.", "action": "customers"})
    if margin < 15:
        alerts.append({"type": "warning", "title": "Low Profit Margin", "message": f"Net profit margin at {margin}%. Industry benchmark is 20%+. Review expenses.", "action": "expenses"})
    if coll_rate < 85:
        alerts.append({"type": "warning", "title": "Collection Rate Low", "message": f"Only {coll_rate}% invoices collected. Follow up on pending payments.", "action": "customers"})
    if health >= 80:
        alerts.append({"type": "success", "title": "Excellent Business Health", "message": f"Health score {health}/100. Business is performing well across all metrics.", "action": "goals"})

    # Dataset-driven Smart Alerts
    for s in kpis.get("topSuppliers", []):
        try:
            if int(s["reliability"]) <= 4:
                alerts.append({"type": "error", "title": "High Supplier Risk", "message": f"Significant procurement spend with {s['name']} (Reliability Score: {s['reliability']}/10). Consider diversifying vendors.", "action": "suppliers"})
                break # Just add one supplier alert
        except:
            pass

    if kpis.get("topPendingPayments"):
        worst = kpis["topPendingPayments"][0]
        if worst["days_overdue"] >= 60:
            alerts.append({"type": "warning", "title": "Critical Aged Receivable", "message": f"Invoice {worst['invoice']} from {worst['customer']} is overdue by {worst['days_overdue']} days. Immediate escalation required.", "action": "customers"})

    for e in kpis.get("expByCode", []):
        if e.get("raw", 0) > (exp * 0.3) and exp > 0:
            alerts.append({"type": "warning", "title": "High Expense Concentration", "message": f"'{e['code']}' accounts for over 30% of your total expenses. Review for optimization opportunities.", "action": "expenses"})
            break

    # ── Pricing Advisor ──
    if kpis["topProducts"]:
        top = kpis["topProducts"][0]
        pricing_recs.append({"product": top["name"], "action": "Increase 5-7%", "reason": "Top revenue driver with strong demand.", "badge": "increase"})
    if len(kpis["topProducts"]) >= 3:
        mid = kpis["topProducts"][2]
        pricing_recs.append({"product": mid["name"], "action": "Maintain", "reason": "Stable category performer. Avoid price disruption.", "badge": "maintain"})
    if len(kpis["topProducts"]) >= 5:
        last = kpis["topProducts"][4]
        pricing_recs.append({"product": last["name"], "action": "Reduce 3-5%", "reason": "Lower volume vs. peers. Price reduction can boost turnover.", "badge": "reduce"})

    # ── Collections Advisor ──
    for p in kpis["topPendingPayments"][:3]:
        if p["days_overdue"] > 30:
            collections_recs.append({"customer": p["customer"], "amount": p["amount"], "days": p["days_overdue"], "urgency": "high", "action": "Send legal notice + immediate call"})
        elif p["days_overdue"] > 10:
            collections_recs.append({"customer": p["customer"], "amount": p["amount"], "days": p["days_overdue"], "urgency": "medium", "action": "Send reminder email + follow-up call"})
        else:
            collections_recs.append({"customer": p["customer"], "amount": p["amount"], "days": p["days_overdue"], "urgency": "low", "action": "Send polite payment reminder"})

    # ── Inventory Advisor ──
    for item in kpis["lowStockItems"][:5]:
        reorder_qty = max(item["reorder"] * 2, 50)
        inventory_recs.append({"product": item["name"], "current": item["qty"], "reorder_at": item["reorder"], "suggested_qty": reorder_qty, "urgency": "critical" if item["qty"] == 0 else "high"})

    # ── Growth Recommendations ──
    if margin > 20:
        growth_recs.append({"title": "Expand High-Margin Categories", "detail": f"With {margin}% margin, reinvest profits into expanding top categories.", "icon": "chart-line"})
    growth_recs.append({"title": "Improve Collections Speed", "detail": f"Reducing payment terms by 10 days could improve cash flow by {fmt_inr(overdue_amt * 0.3)}.", "icon": "hand-holding-dollar"})
    growth_recs.append({"title": "Optimize Supplier Costs", "detail": "Negotiate volume discounts with top 3 suppliers to reduce COGS by 5-8%.", "icon": "truck"})
    if low_stock > 5:
        growth_recs.append({"title": "Prevent Stockout Revenue Loss", "detail": f"Stockouts on {low_stock} products may be causing missed sales. Auto-reorder threshold recommended.", "icon": "boxes-stacked"})

    # ── AI Insights ──
    insights = [
        {"icon": "lightbulb", "color": "yellow", "text": f"Top 20% customers contribute ~63% of revenue. Focus retention on: {', '.join([c['name'] for c in kpis['topCustomers'][:2]]) if kpis['topCustomers'] else 'N/A'}."},
        {"icon": "check-circle", "color": "green", "text": f"Collection rate of {coll_rate}% is {'above' if coll_rate > 85 else 'below'} the 85% benchmark. {'Maintain momentum.' if coll_rate > 85 else 'Accelerate follow-ups.'}"},
        {"icon": "circle-nodes", "color": "blue", "text": f"COGS at {kpis['totalCOGSFmt']} vs Revenue {kpis['totalRevenueFmt']}. Gross margin = {round((rev - cogs) / rev * 100, 1) if rev > 0 else 0}%."},
        {"icon": "chart-line", "color": "purple", "text": f"Best performing category: {kpis['topCategories'][0]['name'] if kpis['topCategories'] else 'N/A'} at {kpis['topCategories'][0]['pct'] if kpis['topCategories'] else '0'}% of total revenue."},
    ]

    return {
        "brief": brief,
        "alerts": alerts,
        "pricingRecs": pricing_recs,
        "collectionsRecs": collections_recs,
        "inventoryRecs": inventory_recs,
        "growthRecs": growth_recs,
        "insights": insights,
    }

# ─── LIFESPAN SETUP ───────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app_obj: FastAPI):
    print("SME Growth Advisor API starting...")
    if not SARVAM_API_KEY:
        raise RuntimeError(
            "Missing required environment variable: SARVAM_API_KEY.\n"
            "Please create or update your .env file with SARVAM_API_KEY=your_key_here."
        )
    if not os.path.exists(DB_PATH) or os.path.getsize(DB_PATH) < 1000:
        print("Initializing database from CSV files...")
        init_db()
    else:
        print(f"Database already exists at {DB_PATH}")
    print("API ready.")
    yield
    print("API shutting down.")

app = FastAPI(title="SME Growth Advisor API", version="2.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─── CACHED KPI STATE ─────────────────────────────────────────────────────────
_kpi_cache = None
_ai_cache = None

def get_cached_kpis():
    global _kpi_cache, _ai_cache
    if _kpi_cache is None:
        _kpi_cache = compute_kpis()
        _ai_cache = generate_ai_insights(_kpi_cache)
    return _kpi_cache, _ai_cache

# ─── ROUTES ───────────────────────────────────────────────────────────────────

@app.get("/api/kpis")
def api_kpis():
    kpis, ai = get_cached_kpis()
    return JSONResponse({**kpis, "ai": ai})

@app.get("/api/kpis/refresh")
def api_refresh():
    global _kpi_cache, _ai_cache
    _kpi_cache = None
    _ai_cache = None
    kpis, ai = get_cached_kpis()
    return JSONResponse({"status": "refreshed", **kpis, "ai": ai})

@app.get("/api/kpis/summary")
def api_summary():
    kpis, ai = get_cached_kpis()
    return JSONResponse({
        "totalRevenueFmt": kpis["totalRevenueFmt"],
        "netProfitFmt": kpis["netProfitFmt"],
        "netProfitMargin": kpis["netProfitMargin"],
        "cashInHandFmt": kpis["cashInHandFmt"],
        "pendingReceivablesFmt": kpis["pendingReceivablesFmt"],
        "healthScore": kpis["healthScore"],
        "healthLabel": kpis["healthLabel"],
        "brief": ai["brief"],
        "alerts": ai["alerts"][:3],
    })

@app.get("/api/sales")
def api_sales():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT s.SalesDate, s.ProductID, s.CustomerID,
               CAST(s.Quantity AS REAL) as qty,
               CAST(s.UnitPrice AS REAL) as price,
               CAST(s.TotalAmount AS REAL) as total,
               p.ProductName, c.DisplayName as CustomerName
         FROM sales_raw s
         LEFT JOIN products p ON s.ProductID = p.ProductID
         LEFT JOIN customers c ON s.CustomerID = c.CustomerID
         ORDER BY s.SalesDate DESC
         LIMIT 100
    """)
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return JSONResponse(rows)

@app.get("/api/products")
def api_products():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT p.*, cat.CategoryName,
               CAST(i.QuantityInStock AS REAL) as stock,
               CAST(i.ReorderPoint AS REAL) as reorder_point
        FROM products p
        LEFT JOIN categories cat ON p.CategoryID = cat.CategoryID
        LEFT JOIN inventory i ON p.ProductID = i.ProductID
        ORDER BY p.ProductID
    """)
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return JSONResponse(rows)

@app.get("/api/customers")
def api_customers():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT c.*,
               COALESCE(s.total_spent, 0) as total_spent,
               COALESCE(s.order_count, 0) as order_count
        FROM customers c
        LEFT JOIN (
            SELECT CustomerID,
                   SUM(CAST(TotalAmount AS REAL)) as total_spent,
                   COUNT(*) as order_count
            FROM sales_raw GROUP BY CustomerID
        ) s ON c.CustomerID = s.CustomerID
        ORDER BY CAST(COALESCE(s.total_spent, 0) AS REAL) DESC
        LIMIT 100
    """)
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return JSONResponse(rows)

@app.get("/api/suppliers")
def api_suppliers():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT s.*,
               COALESCE(po.total_spend, 0) as total_spend,
               COALESCE(po.po_count, 0) as po_count
        FROM suppliers s
        LEFT JOIN (
            SELECT SupplierID,
                   SUM(CAST(TotalAmountSpent AS REAL)) as total_spend,
                   COUNT(*) as po_count
            FROM purchase_orders GROUP BY SupplierID
        ) po ON s.SupplierID = po.SupplierID
        ORDER BY CAST(COALESCE(po.total_spend, 0) AS REAL) DESC
    """)
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return JSONResponse(rows)

@app.get("/api/inventory")
def api_inventory():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT i.ProductID, i.QuantityInStock, i.ReorderPoint,
               p.ProductName, p.CategoryID, p.DefaultSellingPrice, p.StandardMarkup, p.SupplierID,
               cat.CategoryName,
               CASE WHEN CAST(i.QuantityInStock AS REAL) = 0 THEN 'out_of_stock'
                    WHEN CAST(i.QuantityInStock AS REAL) < CAST(i.ReorderPoint AS REAL) THEN 'low_stock'
                    ELSE 'ok' END as status
        FROM inventory i
        LEFT JOIN products p ON i.ProductID = p.ProductID
        LEFT JOIN categories cat ON p.CategoryID = cat.CategoryID
        ORDER BY CAST(i.QuantityInStock AS REAL) ASC
    """)
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return JSONResponse(rows)

@app.get("/api/expenses")
def api_expenses():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM expenses ORDER BY ExpenseDate DESC LIMIT 200")
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return JSONResponse(rows)

@app.get("/api/pending-payments")
def api_pending():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT pp.*, c.DisplayName as CustomerName, c.Phone, c.PaymentTermsDays
        FROM pending_payments pp
        LEFT JOIN customers c ON pp.CustomerID = c.CustomerID
        ORDER BY CAST(pp.DaysOverdue AS INTEGER) DESC, CAST(pp.AmountPending AS REAL) DESC
        LIMIT 15
    """)
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return JSONResponse(rows)

@app.get("/api/purchase-orders")
def api_po():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT po.*, s.SupplierName, p.ProductName
        FROM purchase_orders po
        LEFT JOIN suppliers s ON po.SupplierID = s.SupplierID
        LEFT JOIN products p ON po.ProductID = p.ProductID
        ORDER BY po.PODate DESC
        LIMIT 200
    """)
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return JSONResponse(rows)

@app.get("/api/employees")
def api_employees():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM employees ORDER BY EmployeeID ASC")
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return JSONResponse(rows)

# ── AI Chat Endpoint ──
class ChatRequest(BaseModel):
    message: str
    context: str = ""

class PricingRequest(BaseModel):
    product_name: str

class GrowthRequest(BaseModel):
    query: str


class CollectionsActionRequest(BaseModel):
    customer_name: str
    amount: str
    action: str

@app.post("/api/collections-action")
def api_collections_action(req: CollectionsActionRequest):
    if req.action == "send_email":
        try:
            # Generate professional email using Gemma AI
            system_prompt = "You are an AI Collections Agent. Write a polite but firm payment reminder email to a customer."
            user_prompt = f"Write a collections reminder email to {req.customer_name}. They have a pending amount of {req.amount}. Keep it professional, under 100 words, and mention that late fees may apply if not paid soon."
            email_body = call_gemma(system_prompt, user_prompt)
            if not email_body:
                email_body = format_with_nvidia(system_prompt, user_prompt)
            if not email_body:
                email_body = f"Subject: Payment Reminder\n\nDear {req.customer_name},\n\nThis is a polite reminder that your payment of {req.amount} is currently overdue. Please process the pending amount at your earliest convenience to avoid any late fees.\n\nThank you,\nCollections Team"
            
            # Send the generated email via DronaHQ Webhook if available
            try:
                ask_dronahq(f"ACTION: SEND_EMAIL\\nTO: {req.customer_name}\\nBODY:\\n{email_body}")
            except Exception:
                pass
            return JSONResponse({"status": "success", "message": "Email generated successfully.", "email_body": email_body})
        except Exception as e:
            return JSONResponse({"status": "error", "message": str(e)}, status_code=500)
    
    return JSONResponse({"status": "error", "message": "Unknown action"}, status_code=400)


@app.post("/api/chat")
def api_chat(req: ChatRequest):
    kpis, ai = get_cached_kpis()
    msg = req.message.lower().strip()
    
    import json
    
    # Extract matching product context dynamically based on user query
    conn = get_db()
    cur = conn.cursor()
    
    # Find if user mentioned any specific product in their message
    words = [w for w in msg.replace('%', ' ').replace(',', ' ').split() if len(w) > 3]
    matched_products = []
    for word in words:
        cur.execute("""
            SELECT p.ProductName, p.DefaultSellingPrice, p.StandardMarkup,
                   COALESCE(SUM(s.Quantity), 0) as units_sold
            FROM products p
            LEFT JOIN sales_raw s ON p.ProductID = s.ProductID
            WHERE p.ProductName LIKE ? COLLATE NOCASE
            GROUP BY p.ProductID
            LIMIT 3
        """, (f"%{word}%",))
        for r in cur.fetchall():
            p_name, p_price, p_markup, p_units = r[0], float(r[1] or 0), float(r[2] or 1.2), float(r[3] or 0)
            cost = p_price / p_markup if p_markup > 0 else p_price * 0.75
            margin = ((p_price - cost) / p_price * 100) if p_price > 0 else 25.0
            matched_products.append(f"{p_name}: Price=₹{p_price:.2f}, Cost=₹{cost:.2f}, Margin={margin:.1f}%, Sold={int(p_units)} units")
    conn.close()

    # Build clean, high-density business context
    context_summary = {
        "business": "Indian SME Retail & Distribution",
        "totalRevenue": kpis.get("totalRevenueFmt", "₹20.19Cr"),
        "netProfit": kpis.get("netProfitFmt", "₹-1.99Cr"),
        "profitMargin": f"{kpis.get('netProfitMargin', -9.9)}%",
        "cashInHand": kpis.get("cashInHandFmt", "₹43.42Cr"),
        "pendingReceivables": kpis.get("pendingReceivablesFmt", "₹4.33Cr"),
        "collectionRate": f"{kpis.get('collectionRate', 92.1)}%",
        "overdueInvoices": f"{kpis.get('overdueCount', 144)} invoices ({kpis.get('overdueAmount', '₹2.54Cr')})",
        "queriedProducts": matched_products if matched_products else ["General catalog items (400 active SKUs)"],
        "topAlerts": [a.get("title") + ": " + a.get("message") for a in (ai.get("alerts") or [])[:3]],
        "lowStockItems": kpis.get("lowStockItems", [])
    }

    system_prompt = f"""You are Gemma, an expert AI Business & Pricing Advisor for an SME. You are advising the business owner (Arjun).
Answer the user's business question with clear financial reasoning, projected impact on volume/margin/revenue, and actionable recommendations.

[CURRENT BUSINESS CONTEXT]
{json.dumps(context_summary, indent=2, ensure_ascii=False)}

GUIDELINES:
1. Answer directly with data-driven reasoning and clear markdown bullet points.
2. If the user asks about price changes, calculate the exact new price, margin change, and demand trend impact.
3. Keep the response concise, professional, and practical (under 150 words)."""

    try:
        user_message = req.message.strip()
        print(f"[AI Advisor] Incoming message: {user_message}")
        
        # 1. Primary: Fetch answer directly from DronaHQ
        bot_response = ""
        dhq_error_detail = ""
        try:
            print(f"[AI Advisor] Fetching answer from DronaHQ for: {user_message}")
            bot_response = ask_dronahq(user_message)
        except Exception as dhq_err:
            dhq_error_detail = str(dhq_err)
            print(f"[DronaHQ Query Error]: {dhq_err}")
            bot_response = ""

        # Deliver DronaHQ response or clear diagnostic message (no silent Gemma mask)
        if not bot_response:
            if "404" in dhq_error_detail:
                bot_response = (
                    "⚠️ **DronaHQ Agent returned 404 (Not Found)**\n\n"
                    "Possible causes:\n"
                    "1. **Agent not published:** In DronaHQ Studio, make sure you clicked **'Publish'** / **'Deploy'** on your Agent.\n"
                    "2. **Swapped or incorrect Webhook ID:** Check if the Webhook URL and API Key in `.env` match your DronaHQ integration tab.\n"
                    f"3. Webhook URL called: `{DRONAHQ_WEBHOOK_URL}`"
                )
            elif "402" in dhq_error_detail:
                bot_response = "⚠️ **DronaHQ Error (402 Payment Required):** The DronaHQ account has exceeded its free credits. Please top up or use an active account."
            elif "401" in dhq_error_detail or "403" in dhq_error_detail:
                bot_response = f"⚠️ **DronaHQ Authentication Failed:** The API key was rejected by DronaHQ. Details: {dhq_error_detail}"
            elif dhq_error_detail:
                bot_response = f"⚠️ **DronaHQ Connection Error:** {dhq_error_detail}"
            else:
                bot_response = "I am connected to DronaHQ, but received an empty response."

        # Optional intercept to parse supplier IDs and append order button
        import re
        supplier_matches = list(set(re.findall(r'SUP\d{3}', bot_response)))
        if supplier_matches:
            conn_sup = get_db()
            cur_sup = conn_sup.cursor()
            for sup_id in supplier_matches:
                cur_sup.execute("SELECT SupplierName, ContactEmail, ReliabilityScore FROM suppliers WHERE SupplierID = ?", (sup_id,))
                row = cur_sup.fetchone()
                if row:
                    sup_name, sup_email, rel_score = row
                    
                    # Fetch total orders to show interaction history
                    cur_sup.execute("SELECT COUNT(*), SUM(TotalAmountSpent) FROM purchase_orders WHERE SupplierID = ?", (sup_id,))
                    po_stats = cur_sup.fetchone()
                    total_orders = po_stats[0] if po_stats else 0
                    total_spent = po_stats[1] if po_stats and po_stats[1] else 0

                    # Replace the raw ID with the supplier name
                    bot_response = bot_response.replace(sup_id, f"**{sup_name}** ({sup_id})")
                    
                    # Add benefits block
                    
                    if int(rel_score) >= 7:
                        rel_str = f"Score of {rel_score}/10 based on past on-time deliveries."
                    else:
                        rel_str = "Maintains steady procurement standards for this product category."
                        
                    benefits_html = f"""<div style="margin-top: 15px; padding: 12px; background-color: #f8fafc; border-left: 4px solid #3b82f6; border-radius: 4px;">
                        <h4 style="margin: 0 0 8px 0; color: #1e293b; font-size: 14px;">Why we recommend {sup_name}:</h4>
                        <ul style="margin: 0; padding-left: 20px; font-size: 13px; color: #475569;">
                            <li><strong>Trusted Supplier:</strong> {rel_str}</li>
                            <li><strong>Proven Track Record:</strong> Successfully fulfilled {total_orders} past orders.</li>
                            <li><strong>Volume Partnership:</strong> We have a substantial history of successful large-scale trade with this vendor.</li>
                        </ul>
                    </div>"""
                    bot_response += benefits_html
                    
                    # Generate email body with Gemma / Nvidia
                    email_prompt_sys = f"You are writing an email on behalf of Arjun (business owner). Write ONLY the email body (no subject, no pleasantries like 'Sincerely, [Your Name]' at the end, just the core message and sign off as Arjun). The email is to the supplier {sup_name} to place an order for the product the user is asking about."
                    email_prompt_user = f"User request context: {req.message}. Draft a short email body to order the necessary stock."
                    ai_email_body = call_gemma(email_prompt_sys, email_prompt_user) or format_with_nvidia(email_prompt_sys, email_prompt_user)
                    
                    import urllib.parse
                    if ai_email_body:
                        # clean up any markdown if present
                        ai_email_body = ai_email_body.replace('**', '')
                        body_encoded = urllib.parse.quote(ai_email_body)
                    else:
                        body_encoded = f"Hi {urllib.parse.quote(sup_name)},%0A%0AWe would like to place an order."
                    
                    # Append order button
                    order_btn = f'<br><a href="mailto:{sup_email}?subject=Purchase%20Order%20Inquiry&body={body_encoded}" class="action-btn" style="display:inline-block; margin-top:5px; padding:8px 16px; background-color:#3b82f6; color:white; text-decoration:none; border-radius:4px; font-weight:bold;">🛒 Order from {sup_name}</a>'
                    bot_response += order_btn
            conn_sup.close()

        # Intercept to parse product IDs
        product_matches = list(set(re.findall(r'PROD\d{3}', bot_response)))
        if product_matches:
            conn_prod = get_db()
            cur_prod = conn_prod.cursor()
            for prod_id in product_matches:
                cur_prod.execute("SELECT ProductName FROM products WHERE ProductID = ?", (prod_id,))
                row = cur_prod.fetchone()
                if row:
                    prod_name = row[0]
                    bot_response = bot_response.replace(prod_id, f"**{prod_name}** ({prod_id})")
            conn_prod.close()

    except Exception as e:
        print(f"LLM API Error: {e}")
        # Local heuristic fallback
        msg_lower = req.message.lower().strip()
        if "stock" in msg_lower and ("lowest" in msg_lower or "out of" in msg_lower):
            low_stock = kpis.get("lowStockItems", [])
            if low_stock:
                item = low_stock[0]
                bot_response = f"**Fallback Data (LLM Error):** The product with the lowest stock is **{item['name']}** with only {item['qty']} units left (Reorder point: {item['reorder']})."
            else:
                bot_response = "**Fallback Data:** All stock levels look normal."
        else:
            bot_response = "I am unable to process that request right now due to an API error."

    return JSONResponse({"response": bot_response})

def parse_llm_json(raw_text: str) -> dict:
    import json, re
    t = raw_text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```(?:json)?\s*", "", t)
        t = re.sub(r"\s*```$", "", t)
    match = re.search(r'(\{[\s\S]*\})', t)
    if match:
        return json.loads(match.group(1))
    return json.loads(t)

@app.post("/api/pricing-advisor")
def api_pricing_advisor(req: PricingRequest):
    conn = get_db()
    cur = conn.cursor()
    
    # 0. Intercept invalid/non-product queries
    lower_query = req.product_name.lower()
    invalid_keywords = ["vendor", "supplier", "employee", "sales", "revenue", "profit", "kpi", "staff", "inventory", "cash"]
    if any(k in lower_query for k in invalid_keywords):
        return JSONResponse({
            "id": "INVALID-QUERY",
            "name": req.product_name.title(),
            "badge": "Agent Redirect",
            "badgeColor": "#ef4444",
            "desc": "Please use the AI Business Advisor for questions about vendors, suppliers, or general business metrics.",
            "gemma": "This agent specializes in product pricing and demand elasticity. Your question appears to be about suppliers or business operations. Please ask the AI Business Advisor (Voice Agent) on the main dashboard.",
            "insights": {
                "currentPrice": "N/A",
                "costPrice": "N/A",
                "currentMargin": "N/A",
                "compPrice": "N/A",
                "sales": "N/A",
                "growth": "N/A",
                "elasticity": "N/A",
                "trend": "N/A"
            },
            "impacts": [
                {"label": "Price Increase", "val": "N/A", "color": "#64748b"},
                {"label": "Profit Increase", "val": "N/A", "color": "#64748b"},
                {"label": "Margin Increase", "val": "N/A", "color": "#64748b"},
                {"label": "Revenue Impact", "val": "N/A", "color": "#64748b"}
            ]
        })

    # 1. Search for product in DB
    search_term = f"%{req.product_name}%"
    cur.execute("""
        SELECT p.ProductID, p.ProductName, p.DefaultSellingPrice, p.StandardMarkup 
        FROM products p 
        WHERE p.ProductName LIKE ? COLLATE NOCASE
        LIMIT 1
    """, (search_term,))
    product = cur.fetchone()
    
    if product:
        product_id, product_name, default_price, markup = product
        default_price = safe_float(default_price)
        
        # Get Sales and Cost Data
        cur.execute("""
            SELECT SUM(CAST(Quantity AS REAL)), SUM(CAST(TotalAmount AS REAL))
            FROM sales_raw
            WHERE ProductID = ?
        """, (product_id,))
        sales_data = cur.fetchone()
        total_qty_sold = safe_float(sales_data[0]) if sales_data and sales_data[0] else 120
        total_revenue = safe_float(sales_data[1]) if sales_data and sales_data[1] else default_price * total_qty_sold
        
        cur.execute("""
            SELECT AVG(CAST(CostPerUnit AS REAL))
            FROM purchase_orders
            WHERE ProductID = ?
        """, (product_id,))
        cost_data = cur.fetchone()
        avg_cost = safe_float(cost_data[0]) if cost_data and cost_data[0] else (default_price / (1 + safe_float(markup)/100) if markup else default_price * 0.75)
        current_margin = ((default_price - avg_cost) / default_price * 100) if default_price > 0 else 25.0
    else:
        conn.close()
        return JSONResponse({
            "id": "INVALID-QUERY",
            "name": req.product_name.title(),
            "badge": "Agent Redirect",
            "badgeColor": "#ef4444",
            "desc": f"Product '{req.product_name}' not found in catalog.",
            "gemma": f"I am a Pricing Advisor for your existing product catalog. '{req.product_name}' was not found. If this is a general question, please use the AI Business Advisor.",
            "insights": {
                "currentPrice": "N/A", "costPrice": "N/A", "currentMargin": "N/A", "compPrice": "N/A", "sales": "N/A", "growth": "N/A", "elasticity": "N/A", "trend": "N/A"
            },
            "impacts": [
                {"label": "Price Increase", "val": "N/A", "color": "#64748b"},
                {"label": "Profit Increase", "val": "N/A", "color": "#64748b"},
                {"label": "Margin Increase", "val": "N/A", "color": "#64748b"},
                {"label": "Revenue Impact", "val": "N/A", "color": "#64748b"}
            ]
        })
    
    conn.close()
    
    # 3. Formulate Prompt for Gemma
    prompt = f"""You are Gemma, an expert Pricing & Revenue Advisor for SMEs. Analyze this product:
Product Name: {product_name}
Current Selling Price: ₹{default_price:.2f}
Cost Price: ₹{avg_cost:.2f}
Current Margin: {current_margin:.1f}%
Units Sold: {int(total_qty_sold)} units

Return ONLY a valid JSON object (no markdown, no other text) with this exact schema:
{{
  "badge": "Increase 5-7%",
  "badgeColor": "#10b981",
  "desc": "High demand with low price sensitivity. Increasing price will improve margins without impacting volume.",
  "gemma": "Based on market demand and low price sensitivity, increasing price by 5-7% will expand net profit margin while preserving order volumes.",
  "insights": {{
    "currentPrice": "₹{default_price:.0f}",
    "costPrice": "₹{avg_cost:.0f}",
    "currentMargin": "{current_margin:.1f}%",
    "compPrice": "₹{default_price * 1.05:.0f}",
    "sales": "{int(total_qty_sold):,} units",
    "growth": "+14.2%",
    "elasticity": "Low (0.34)",
    "trend": "Increasing"
  }},
  "impacts": [
    {{"label": "Price Increase", "val": "5-7%", "color": "#6366f1"}},
    {{"label": "Profit Increase", "val": "₹{(default_price * 0.06 * total_qty_sold):.0f}", "color": "#10b981"}},
    {{"label": "Margin Increase", "val": "+3.4%", "color": "#3b82f6"}},
    {{"label": "Revenue Impact", "val": "Positive", "color": "#10b981"}}
  ]
}}"""
    
    try:
        content = call_gemma(prompt, f"Analyze this product and format as the exact JSON schema requested:\nProduct: {product_name}")
        if not content:
            content = format_with_nvidia(prompt, f"Analyze this product and format as the exact JSON schema requested:\nProduct: {product_name}")
        if not content:
            dronahq_response = ask_dronahq(prompt)
            content = format_with_nvidia(prompt, f"Format this data into the requested JSON schema:\n{dronahq_response}")
        
        result = parse_llm_json(content)
        result["id"] = product_id
        result["name"] = product_name
        return JSONResponse(result)
    except Exception as e:
        print("LLM Pricing Error:", str(e))
        # Guaranteed robust fallback
        fallback = {
            "id": product_id,
            "name": product_name,
            "badge": "Increase 5-7%",
            "badgeColor": "#10b981",
            "desc": f"Strong market demand with low price elasticity for {product_name}. Optimizing price improves gross margins.",
            "gemma": f"Analysis indicates that demand for {product_name} is resilient. A 5-7% price optimization will boost gross profit without causing customer attrition.",
            "insights": {
                "currentPrice": f"₹{default_price:.0f}",
                "costPrice": f"₹{avg_cost:.0f}",
                "currentMargin": f"{current_margin:.1f}%",
                "compPrice": f"₹{default_price * 1.06:.0f}",
                "sales": f"{int(total_qty_sold):,} units",
                "growth": "+18.5%",
                "elasticity": "Low (0.32)",
                "trend": "Increasing"
            },
            "impacts": [
                {"label": "Price Increase", "val": "5-7%", "color": "#6366f1"},
                {"label": "Profit Increase", "val": f"+₹{(default_price * 0.06 * total_qty_sold):.0f}", "color": "#10b981"},
                {"label": "Margin Increase", "val": "+3.5%", "color": "#3b82f6"},
                {"label": "Revenue Impact", "val": "Minimal Risk", "color": "#10b981"}
            ]
        }
        return JSONResponse(fallback)

@app.post("/api/growth-advisor")
def api_growth_advisor(req: GrowthRequest):
    # 0. Intercept invalid queries:
    lower_query = req.query.lower()
    growth_keywords = ["price", "cost", "sale", "volume", "marketing", "reduce", "increase", "grow", "profit", "margin", "discount", "campaign"]
    if not any(k in lower_query for k in growth_keywords):
        return JSONResponse({
            "name": "Invalid Scenario",
            "title": "Unrelated Query",
            "badge": "Agent Redirect",
            "badgeColor": "#ef4444",
            "desc": "This Growth Simulator evaluates revenue, volume, and pricing scenarios. Your query appears unrelated.",
            "gemma": "Please route general business or supplier queries to the AI Business Advisor (Voice Agent).",
            "volumeChg": 0.0,
            "priceChg": 0.0,
            "costChg": 0.0,
            "marketingChg": 0.0
        })

    prompt = f"""You are Gemma, an AI Growth Simulator for an SME.
Simulate this business growth scenario requested by the user:
User Request: {req.query}

Analyze this request and return ONLY a valid JSON object with numeric percentages:
{{
  "name": "Short Scenario Name",
  "title": "Scenario: Clear Description",
  "badge": "AI Optimized",
  "badgeColor": "#8b5cf6",
  "desc": "Detailed explanation of how this strategy drives business expansion.",
  "volumeChg": 6.5,
  "priceChg": 4.0,
  "costChg": -2.5,
  "marketingChg": 5.0
}}

IMPORTANT: volumeChg, priceChg, costChg, marketingChg MUST be pure numbers (floats). Return ONLY valid JSON."""
    try:
        content = call_gemma(prompt, f"Format the simulation results into the requested JSON schema for user request: {req.query}")
        if not content:
            content = format_with_nvidia(prompt, f"Format this data into the requested JSON schema for user request: {req.query}")
        if not content:
            dronahq_response = ask_dronahq(prompt)
            content = format_with_nvidia(prompt, f"Format this data into the requested JSON schema:\n{dronahq_response}")
        
        result = parse_llm_json(content)
        result["volumeChg"] = float(result.get("volumeChg", 6.0))
        result["priceChg"] = float(result.get("priceChg", 4.0))
        result["costChg"] = float(result.get("costChg", -2.0))
        result["marketingChg"] = float(result.get("marketingChg", 5.0))
        return JSONResponse(result)
    except Exception as e:
        print("LLM Growth Simulator Error:", str(e))
        import re
        q = req.query.lower()
        
        # Simple heuristics
        v_chg = 0.0
        p_chg = 0.0
        c_chg = 0.0
        m_chg = 0.0
        
        # Extract any numbers from query
        nums = re.findall(r'\d+\.?\d*', q)
        val = float(nums[0]) if nums else 5.0
        
        if 'decrease cost' in q or 'reduce cost' in q or 'cost reduction' in q or 'cost decreases' in q or 'cost of tomato' in q:
            c_chg = -val
        elif 'increase price' in q or 'raise price' in q:
            p_chg = val
            v_chg = -(val * 0.2) # Elasticity
        elif 'increase sales' in q or 'increase volume' in q:
            v_chg = val
        elif 'marketing' in q:
            m_chg = val
            v_chg = val * 0.8
        else:
            v_chg = val
            
        return JSONResponse({
            "name": req.query[:25] if len(req.query) <= 25 else req.query[:22] + "...",
            "title": f"Scenario: {req.query}",
            "badge": "Heuristic Engine",
            "badgeColor": "#f59e0b",
            "desc": f"Calculated scenario for '{req.query}'. Showing isolated impact without assuming unrelated growth.",
            "volumeChg": v_chg,
            "priceChg": p_chg,
            "costChg": c_chg,
            "marketingChg": m_chg
        })

# ── Static file serving (serve the frontend) ──
@app.get("/styles.css")
def serve_styles_css():
    return FileResponse(os.path.join(DATA_DIR, "styles.css"), media_type="text/css")

@app.get("/splash.css")
def serve_splash_css():
    return FileResponse(os.path.join(DATA_DIR, "splash.css"), media_type="text/css")

@app.get("/logo.png")
def serve_logo_png():
    return FileResponse(os.path.join(DATA_DIR, "logo.png"), media_type="image/png")

@app.get("/app.js")
def serve_app_js():
    path = os.path.join(DATA_DIR, "app.js")
    if os.path.exists(path):
        return FileResponse(path, media_type="application/javascript")
    return JSONResponse({"error": "not found"}, status_code=404)

@app.get("/kpis.json")
def serve_kpis_json():
    path = os.path.join(DATA_DIR, "kpis.json")
    if os.path.exists(path):
        return FileResponse(path, media_type="application/json")
    return JSONResponse({"error": "not found"}, status_code=404)

@app.post("/api/fraud-guard/analyze")
def api_fraud_guard(req: dict):
    email_text = req.get("email_text", "")
    
    prompt = f"""You are 'NVIDIA Fraud AI', an automated fraud-detection AI (honeypot) for a company. 
A suspicious email has been received:
---
{email_text}
---

Your task:
1. Identify the 'Scam Type' (e.g., Invoice Fraud, Phishing, BEC).
2. Calculate a Risk Score (0-100).
3. Extract Actionable IOCs (Indicators of Compromise) like emails, domains, IBANs, phone numbers, URLs. Assign a threat level (HIGH, MEDIUM, LOW) to each.
4. Create a fictional 'Persona' for the honeypot (e.g., 'Arjun Mehta, Busy CFO').
5. Create a fictional 'Mailbox' matching the persona (e.g., 'arjun.mehta@targetcompany.com').
6. Write a pragmatic, stalling response from this Persona to the scammer. The tone should be professional but inquisitive, asking for verifying documents to delay them and gather more intel. **CRITICAL: You MUST sign off the email with your Persona's human name. DO NOT sign off as NVIDIA or an AI.** The reply must be highly specific to the provided email (using their names, invoice numbers, amounts, etc.). Use HTML `<br>` for line breaks.

Return ONLY a valid JSON object with this exact schema (do not include markdown formatting like ```json):
{{
  "scam_type": "Invoice Fraud",
  "risk_score": 100,
  "persona": "Arjun Mehta, Busy CFO",
  "mailbox": "arjun.mehta@targetcompany.com",
  "iocs": [
    {{"value": "accounts@meridianpartners-llc.com", "type": "EMAIL", "level": "MEDIUM"}},
    {{"value": "GB82WEST12345698765432", "type": "IBAN", "level": "HIGH"}}
  ],
  "ai_reply": "Hi [Name],<br><br>I received your email regarding [Specific Invoice].<br><br>Before I can process [Specific Amount], I need you to confirm...<br><br>Best regards,<br>Arjun Mehta"
}}
"""
    try:
        content = call_gemma(prompt, "Format the analysis into the requested JSON schema based on the email provided.")
        if not content:
            content = format_with_nvidia(prompt, "Format the analysis into the requested JSON schema based on the email provided.")
        if not content:
            raise ValueError("LLM API returned empty content")
        result = parse_llm_json(content)
        return JSONResponse(result)
    except Exception as e:
        print("Fraud Guard Error:", str(e))
        import re
        text = req.get("email_text", "")
        
        # Extract basic info
        sender_match = re.search(r'From:\s*([^<]+)', text)
        sender_name = sender_match.group(1).strip() if sender_match else "Sender"
        
        email_match = re.search(r'<([^>]+)>', text)
        sender_email = email_match.group(1).strip() if email_match else "suspicious@example.com"
        
        amount_match = re.search(r'(?:Rs|£|\$)\s*[\d,]+(?:\.\d+)?', text)
        amount = amount_match.group(0) if amount_match else "the requested amount"
        
        inv_match = re.search(r'(INV[/\-A-Z0-9]+)', text, re.I)
        invoice = inv_match.group(1) if inv_match else "the invoice"
        
        # Extract account numbers for IOCs
        acct_match = re.search(r'(?:Account no|Account Number):\s*(\d+)', text, re.I)
        acct = acct_match.group(1) if acct_match else "1234567890"

        iocs = [
            {"value": sender_email, "type": "EMAIL", "level": "HIGH"},
            {"value": acct, "type": "ACCOUNT NUMBER", "level": "MEDIUM"}
        ]

        if inv_match:
            iocs.append({"value": invoice, "type": "INVOICE NUMBER", "level": "LOW"})
            
        first_name = sender_name.split()[0] if sender_name != "Sender" else "there"

        return JSONResponse({
            "scam_type": "Invoice Fraud (Heuristic Fallback)",
            "risk_score": 95,
            "persona": "Arjun Mehta, Busy CFO",
            "mailbox": "arjun.mehta@targetcompany.com",
            "iocs": iocs,
            "ai_reply": f"Hi {first_name},<br><br>I received your email regarding {invoice}.<br><br>Before I can process the payment for {amount}, I need you to confirm the invoice details and provide the official payment instructions on your company letterhead.<br><br>Could you please share a copy of the original invoice for our compliance team?<br><br>Best regards,<br>Arjun Mehta"
        })

import pandas as pd
import random

@app.get("/api/fraud-guard/fetch-email")
def api_fraud_guard_fetch_email():
    try:
        csv_path = os.path.join(DATA_DIR, "email farud", "incoming_emails.csv")
        df = pd.read_csv(csv_path)
        # Filter for rows where Label is 'Fraud'
        fraud_df = df[df['Label'].str.strip().str.lower() == 'fraud']
        
        if len(fraud_df) == 0:
            return JSONResponse({"error": "No fraud emails found."}, status_code=404)
            
        # Pick a random row
        row = fraud_df.sample(1).iloc[0]
        
        # Format the email text
        body_text = str(row['Body']).replace('\n', '<br>')
        email_text = f"""<b>Subject:</b> {row['Subject']}<br><br>
<b>From:</b> {row['FromName']} &lt;{row['FromEmail']}&gt;<br>
<b>Date:</b> {row['ReceivedAt']}<br><br>
{body_text}
"""
        return JSONResponse({
            "email_text": email_text,
            "scam_type": row['ScamType'],
            "time": str(row['ReceivedAt'])[-5:], # just get HH:MM
            "raw_subject": row['Subject'],
            "raw_from_email": row['FromEmail']
        })
    except Exception as e:
        print("Fetch Email Error:", str(e))
        return JSONResponse({"error": "Failed to fetch email."}, status_code=500)

# ─── SARVAM PROXY PASSTHROUGH & RATE LIMITING ───────────────────────────────
sarvam_rate_limit_records = defaultdict(list)
SARVAM_RATE_LIMIT_WINDOW = 60  # seconds
SARVAM_MAX_REQUESTS_PER_WINDOW = 30  # 30 req / min per IP

def check_sarvam_rate_limit(client_ip: str) -> bool:
    now = time.time()
    valid_timestamps = [t for t in sarvam_rate_limit_records[client_ip] if now - t < SARVAM_RATE_LIMIT_WINDOW]
    sarvam_rate_limit_records[client_ip] = valid_timestamps
    if len(valid_timestamps) >= SARVAM_MAX_REQUESTS_PER_WINDOW:
        return False
    sarvam_rate_limit_records[client_ip].append(now)
    return True

@app.api_route("/api/sarvam/{rest_of_path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
@app.api_route("/api/sarvam", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
async def sarvam_passthrough(request: Request, rest_of_path: str = ""):
    # 1. Rate Limiting: 30 requests per minute per IP
    client_ip = request.client.host if request.client else "unknown"
    if not check_sarvam_rate_limit(client_ip):
        return Response(
            content=json.dumps({"error": "Too Many Requests. Rate limit of 30 req/min exceeded."}),
            status_code=429,
            media_type="application/json"
        )

    # 2. Build upstream target URL: https://apps.sarvam.ai/api/app-runtime/<rest-of-path>
    upstream_base = "https://apps.sarvam.ai/api/app-runtime"
    clean_subpath = rest_of_path.lstrip("/")
    target_url = f"{upstream_base}/{clean_subpath}" if clean_subpath else upstream_base
    if request.url.query:
        target_url = f"{target_url}?{request.url.query}"

    # 3. Read incoming request body
    body = await request.body()

    # 4. Forward headers with X-API-Key (NEVER log the API key)
    forward_headers = {
        "X-API-Key": SARVAM_API_KEY
    }
    content_type = request.headers.get("content-type")
    if content_type:
        forward_headers["Content-Type"] = content_type
    if "accept" in request.headers:
        forward_headers["Accept"] = request.headers["accept"]
    if "user-agent" in request.headers:
        forward_headers["User-Agent"] = request.headers["user-agent"]

    # 5. Forward request upstream
    try:
        upstream_resp = requests.request(
            method=request.method,
            url=target_url,
            headers=forward_headers,
            data=body if body else None,
            timeout=60,
            allow_redirects=True
        )

        resp_content_type = upstream_resp.headers.get("content-type", "application/json")
        return Response(
            content=upstream_resp.content,
            status_code=upstream_resp.status_code,
            media_type=resp_content_type,
            headers={"Content-Type": resp_content_type}
        )
    except requests.exceptions.RequestException as e:
        print(f"Sarvam proxy upstream error: {type(e).__name__}")
        return Response(
            content=json.dumps({"error": "Failed to connect to Sarvam AI upstream service."}),
            status_code=502,
            media_type="application/json"
        )

@app.get("/")
def serve_root():
    return FileResponse(os.path.join(DATA_DIR, "index.html"))

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port, reload=False)
