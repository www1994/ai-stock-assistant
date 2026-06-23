from __future__ import annotations

import json
import multiprocessing as mp
import os
import queue as queue_module
import re
import sys
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

import akshare as ak
import pandas as pd
from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, jsonify, render_template, request
from openai import OpenAI


BASE_DIR = Path(__file__).resolve().parent
CACHE_FILE = BASE_DIR / "data" / "cache.json"
CN_TZ = ZoneInfo("Asia/Shanghai")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o")
MAX_SCREEN_RESULTS = int(os.getenv("MAX_SCREEN_RESULTS", "8"))

app = Flask(__name__)
scheduler_instance: BackgroundScheduler | None = None


def now_cn() -> datetime:
    return datetime.now(CN_TZ)


def today_text() -> str:
    return now_cn().strftime("%Y-%m-%d")


def load_cache() -> dict[str, Any]:
    if not CACHE_FILE.exists():
        return {}
    try:
        return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_cache(cache: dict[str, Any]) -> None:
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")


def fetch_worker(queue: Any, fetcher_name: str, args: tuple[Any, ...], kwargs: dict[str, Any]) -> None:
    """子进程里执行 AKShare；主进程超时后可直接终止子进程。"""
    try:
        fetcher = getattr(ak, fetcher_name)
        queue.put(("ok", fetcher(*args, **kwargs)))
    except Exception as exc:
        queue.put(("err", str(exc)))


def safe_fetch(fetcher: Callable[..., Any], *args: Any, timeout: int = 20, **kwargs: Any) -> tuple[Any | None, str | None]:
    """给 AKShare 请求加超时，避免单个数据源卡住整个页面。"""
    fetcher_name = getattr(fetcher, "__name__", "")
    if not fetcher_name or not hasattr(ak, fetcher_name):
        return None, "只支持 AKShare 数据函数"

    ctx_name = "spawn" if sys.platform == "darwin" else "fork"
    ctx = mp.get_context(ctx_name)
    queue = ctx.Queue(maxsize=1)
    process = ctx.Process(target=fetch_worker, args=(queue, fetcher_name, args, kwargs))
    process.start()
    process.join(timeout)

    if process.is_alive():
        process.terminate()
        process.join(2)
        return None, f"{fetcher_name} 超时"

    try:
        status, payload = queue.get(timeout=2)
    except queue_module.Empty:
        return None, f"{fetcher_name} 无返回，退出码 {process.exitcode}"

    if status == "ok":
        return payload, None
    return None, f"{fetcher_name} 失败：{payload}"


def first_col(df: pd.DataFrame, candidates: list[str]) -> str | None:
    for col in candidates:
        if col in df.columns:
            return col
    return None


def to_number(value: Any) -> float | None:
    if value is None or pd.isna(value):
        return None
    text = str(value).replace("%", "").replace(",", "").replace("--", "").replace("—", "").strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def series_to_number(series: pd.Series) -> pd.Series:
    return pd.to_numeric(
        series.astype(str)
        .str.replace("%", "", regex=False)
        .str.replace(",", "", regex=False)
        .str.replace("--", "", regex=False)
        .str.replace("—", "", regex=False),
        errors="coerce",
    )


def is_main_board(code: str) -> bool:
    return code.startswith("60") or code.startswith("000")


def has_openai_key() -> bool:
    return bool(os.getenv("OPENAI_API_KEY"))


def call_openai_json(prompt: str, fallback: dict[str, Any] | list[Any]) -> dict[str, Any] | list[Any]:
    """调用 OpenAI；没配置 key 或接口失败时返回本地兜底结果。"""
    if not has_openai_key():
        return fallback

    try:
        client = OpenAI()
        response = client.responses.create(
            model=OPENAI_MODEL,
            input=[
                {
                    "role": "system",
                    "content": (
                        "你是A股交易复盘助手。只输出有效JSON，不要Markdown。"
                        "必须用中文，结论要短，必须提示不构成投资建议。"
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            temperature=0.2,
            max_output_tokens=900,
        )
        text = getattr(response, "output_text", "") or ""
        return extract_json(text, fallback)
    except Exception as exc:
        wrapped = fallback.copy() if isinstance(fallback, dict) else fallback
        if isinstance(wrapped, dict):
            wrapped["ai_error"] = str(exc)
        return wrapped


def extract_json(text: str, fallback: dict[str, Any] | list[Any]) -> dict[str, Any] | list[Any]:
    text = text.strip()
    try:
        return json.loads(text)
    except Exception:
        pass

    match = re.search(r"(\{.*\}|\[.*\])", text, re.S)
    if not match:
        return fallback
    try:
        return json.loads(match.group(1))
    except Exception:
        return fallback


def normalize_news(df: pd.DataFrame, source: str, limit: int = 15) -> list[dict[str, str]]:
    if df is None or df.empty:
        return []

    title_col = first_col(df, ["标题", "title", "新闻标题", "内容", "摘要"])
    content_col = first_col(df, ["内容", "摘要", "summary", "新闻内容", "标题"])
    time_col = first_col(df, ["发布时间", "发布日期", "时间", "date", "datetime"])
    if title_col is None:
        return []

    news: list[dict[str, str]] = []
    for _, row in df.head(limit).iterrows():
        title = str(row.get(title_col, "")).strip()
        content = str(row.get(content_col, "")).strip() if content_col else title
        published = str(row.get(time_col, "")).strip() if time_col else ""
        if title:
            news.append(
                {
                    "source": source,
                    "title": title[:120],
                    "content": content[:240],
                    "published_at": published,
                }
            )
    return news


def fetch_latest_news() -> tuple[list[dict[str, str]], list[str]]:
    warnings: list[str] = []
    news: list[dict[str, str]] = []

    cls_df, err = safe_fetch(ak.stock_info_global_cls, symbol="全部", timeout=18)
    if err:
        warnings.append(err)
    else:
        news.extend(normalize_news(cls_df, "财联社", limit=18))

    sina_df, err = safe_fetch(ak.stock_info_global_sina, timeout=18)
    if err:
        warnings.append(err)
    else:
        news.extend(normalize_news(sina_df, "新浪财经", limit=18))

    # 个别环境新浪会失败，补一个东方财富兜底，保证盘前页至少有材料。
    if len(news) < 8:
        em_df, err = safe_fetch(ak.stock_news_em, symbol="000001", timeout=12)
        if err:
            warnings.append(err)
        else:
            news.extend(normalize_news(em_df, "东方财富", limit=10))

    return news[:30], warnings


def keyword_fallback_news(news: list[dict[str, str]], warnings: list[str]) -> dict[str, Any]:
    text = "；".join(item["title"] for item in news[:12])
    sector_words = ["机器人", "医药", "证券", "有色", "PCB", "算力", "AI", "消费", "银行", "地产"]
    sectors = [word for word in sector_words if word.lower() in text.lower()]
    stock_matches = re.findall(r"(?<!\d)(?:60\d{4}|000\d{3})(?!\d)", text)
    if sectors or stock_matches:
        sector_text = "、".join(sectors[:4]) if sectors else "公司公告"
        stock_text = "；主板代码：" + "、".join(stock_matches[:4]) if stock_matches else ""
        summary = f"最新资讯集中在{sector_text}{stock_text}。先看指数承接和主线持续性，不追高。"
    else:
        summary = "最新资讯偏宏观、海外和公司公告，主板机会暂不清晰。先看指数承接，不追高。"
    return {
        "summary": summary[:100],
        "positive": [],
        "negative": [],
        "sectors": sectors[:6],
        "stocks": stock_matches[:8],
        "focus": "先看指数承接，再看主线持续性，不追高。",
        "warnings": warnings,
        "ai_enabled": has_openai_key(),
    }


def build_pre_market(refresh: bool = False) -> dict[str, Any]:
    cache = load_cache()
    cached = cache.get("pre_market", {})
    if not refresh and cached.get("date") == today_text():
        return cached

    news, warnings = fetch_latest_news()
    fallback = keyword_fallback_news(news, warnings)
    prompt = json.dumps(
        {
            "任务": "根据最新财经资讯，分类利好/利空，生成100字以内A股盘前摘要，提取影响板块和个股。",
            "输出JSON字段": ["summary", "positive", "negative", "sectors", "stocks", "focus"],
            "限制": "summary必须100字以内；个股只保留60或000开头主板股票；不得推荐ST、创业板、科创板、北交所。",
            "资讯": news[:24],
        },
        ensure_ascii=False,
    )
    result = call_openai_json(prompt, fallback)
    if isinstance(result, dict):
        result.setdefault("warnings", warnings)
        result["summary"] = str(result.get("summary", ""))[:100]
        result["ai_enabled"] = has_openai_key()
        result["date"] = today_text()
        result["updated_at"] = now_cn().strftime("%Y-%m-%d %H:%M:%S")
        result["raw_news"] = news[:10]
    else:
        result = fallback

    cache["pre_market"] = result
    save_cache(cache)
    return result


def latest_report_dates(limit: int = 6) -> list[str]:
    today = now_cn()
    report_days = ["0331", "0630", "0930", "1231"]
    dates: list[str] = []
    for year in range(today.year, today.year - 3, -1):
        for day in reversed(report_days):
            date_text = f"{year}{day}"
            if date_text <= today.strftime("%Y%m%d"):
                dates.append(date_text)
    return dates[:limit]


def fetch_revenue_growth() -> tuple[pd.DataFrame, list[str]]:
    warnings: list[str] = []
    for report_date in latest_report_dates():
        df, err = safe_fetch(ak.stock_yjbb_em, date=report_date, timeout=20)
        if err:
            warnings.append(err)
            continue
        if df is None or df.empty:
            continue
        code_col = first_col(df, ["股票代码", "代码"])
        growth_col = first_col(df, ["营业收入-同比增长", "营业总收入同比增长", "营收同比", "营业收入同比增长"])
        if code_col and growth_col:
            return (
                pd.DataFrame(
                    {
                        "code": df[code_col].astype(str).str.zfill(6),
                        "revenue_growth": series_to_number(df[growth_col]),
                    }
                ).drop_duplicates("code"),
                warnings,
            )
    return pd.DataFrame(columns=["code", "revenue_growth"]), warnings or ["业绩报表未取到营收增速字段"]


def fetch_lhb_codes() -> tuple[set[str], list[str]]:
    date = now_cn().strftime("%Y%m%d")
    df, err = safe_fetch(ak.stock_lhb_detail_daily_sina, date=date, timeout=18)
    if err or df is None or df.empty:
        return set(), [err or "今日龙虎榜暂无数据"]
    code_col = first_col(df, ["股票代码", "代码", "证券代码"])
    if not code_col:
        return set(), ["龙虎榜字段未识别"]
    return set(df[code_col].astype(str).str.zfill(6)), []


def fetch_fund_flow() -> tuple[pd.DataFrame, list[str]]:
    df, err = safe_fetch(ak.stock_individual_fund_flow_rank, indicator="今日", timeout=20)
    if err or df is None or df.empty:
        return pd.DataFrame(columns=["code", "main_net_inflow"]), [err or "主力资金接口暂无数据"]

    code_col = first_col(df, ["代码", "股票代码"])
    flow_col = first_col(df, ["今日主力净流入-净额", "主力净流入", "净额", "今日超大单净流入-净额"])
    if not code_col or not flow_col:
        return pd.DataFrame(columns=["code", "main_net_inflow"]), ["主力资金字段未识别"]

    return (
        pd.DataFrame(
            {
                "code": df[code_col].astype(str).str.zfill(6),
                "main_net_inflow": series_to_number(df[flow_col]),
            }
        ).drop_duplicates("code"),
        [],
    )


def normalize_spot(df: pd.DataFrame) -> pd.DataFrame:
    code_col = first_col(df, ["代码", "股票代码"])
    name_col = first_col(df, ["名称", "股票简称"])
    price_col = first_col(df, ["最新价", "现价", "收盘"])
    change_col = first_col(df, ["涨跌幅", "今日涨跌幅"])
    pe_col = first_col(df, ["市盈率-动态", "市盈率", "动态市盈率", "PE"])
    volume_ratio_col = first_col(df, ["量比", "今日量比"])
    market_cap_col = first_col(df, ["总市值", "总市值-元"])
    required = [code_col, name_col, price_col, change_col, pe_col, volume_ratio_col]
    if any(col is None for col in required):
        raise ValueError(f"行情字段不完整：{list(df.columns)}")

    result = pd.DataFrame(
        {
            "code": df[code_col].astype(str).str.zfill(6),
            "name": df[name_col].astype(str),
            "price": series_to_number(df[price_col]),
            "change_pct": series_to_number(df[change_col]),
            "pe": series_to_number(df[pe_col]),
            "volume_ratio": series_to_number(df[volume_ratio_col]),
            "market_cap": series_to_number(df[market_cap_col]) if market_cap_col else pd.NA,
        }
    )
    result = result[result["code"].map(is_main_board)].copy()
    result = result[~result["name"].str.contains("ST|退", case=False, regex=True, na=False)].copy()
    return result.dropna(subset=["price", "change_pct", "pe", "volume_ratio"])


def technical_check(code: str) -> tuple[dict[str, Any] | None, str | None]:
    end_date = now_cn().strftime("%Y%m%d")
    start_date = (now_cn() - timedelta(days=90)).strftime("%Y%m%d")
    hist, err = safe_fetch(
        ak.stock_zh_a_hist,
        symbol=code,
        period="daily",
        start_date=start_date,
        end_date=end_date,
        adjust="qfq",
        timeout=12,
    )
    if err or hist is None or len(hist) < 35:
        return None, err or "历史行情不足"

    close_col = first_col(hist, ["收盘", "收盘价", "close"])
    if close_col is None:
        return None, "历史行情字段未识别"
    closes = series_to_number(hist[close_col]).dropna()
    if len(closes) < 35:
        return None, "历史收盘价不足"

    ema12 = closes.ewm(span=12, adjust=False).mean()
    ema26 = closes.ewm(span=26, adjust=False).mean()
    dif = ema12 - ema26
    dea = dif.ewm(span=9, adjust=False).mean()
    macd_cross = bool(dif.iloc[-2] <= dea.iloc[-2] and dif.iloc[-1] > dea.iloc[-1])
    ma5 = closes.rolling(5).mean().iloc[-1]
    ma10 = closes.rolling(10).mean().iloc[-1]
    ma20 = closes.rolling(20).mean().iloc[-1]
    ma_bull = bool(ma5 > ma10 > ma20)

    return (
        {
            "macd_cross": macd_cross,
            "ma_bull": ma_bull,
            "ma5": round(float(ma5), 2),
            "ma10": round(float(ma10), 2),
            "ma20": round(float(ma20), 2),
        },
        None,
    )


def fallback_stock_comment(row: dict[str, Any]) -> str:
    return (
        f"量比{row.get('volume_ratio', 0):.2f}，主力净流入为正，"
        f"但只适合小仓位跟踪；跌破短期均线需止损。"
    )


def add_ai_comments(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not rows:
        return rows

    fallback = {
        "items": [
            {"code": row["code"], "comment": fallback_stock_comment(row), "risk": "高波动，禁止追高。"}
            for row in rows
        ]
    }
    prompt = json.dumps(
        {
            "任务": "给每只主板股票写一句短点评和一句风险提示。",
            "限制": "不得推荐创业板、科创板、北交所、ST；不要超过35字；输出JSON：{'items':[{'code':'000001','comment':'','risk':''}]}",
            "股票": rows,
        },
        ensure_ascii=False,
    )
    result = call_openai_json(prompt, fallback)
    comments = {}
    if isinstance(result, dict):
        for item in result.get("items", []):
            comments[str(item.get("code", "")).zfill(6)] = item

    for row in rows:
        item = comments.get(row["code"], {})
        row["ai_comment"] = item.get("comment") or fallback_stock_comment(row)
        row["risk"] = item.get("risk") or "题材退潮或指数走弱时先减仓。"
    return rows


def run_screen(sort: str = "main_flow") -> dict[str, Any]:
    warnings: list[str] = []
    spot_df, err = safe_fetch(ak.stock_zh_a_spot_em, timeout=25)
    if err or spot_df is None or spot_df.empty:
        return {"date": today_text(), "updated_at": now_cn().strftime("%Y-%m-%d %H:%M:%S"), "items": [], "warnings": [err or "实时行情为空"]}

    try:
        candidates = normalize_spot(spot_df)
    except Exception as exc:
        return {"date": today_text(), "updated_at": now_cn().strftime("%Y-%m-%d %H:%M:%S"), "items": [], "warnings": [str(exc)]}

    revenue_df, rev_warnings = fetch_revenue_growth()
    fund_df, fund_warnings = fetch_fund_flow()
    lhb_codes, lhb_warnings = fetch_lhb_codes()
    warnings.extend(rev_warnings + fund_warnings + lhb_warnings)

    candidates = candidates.merge(revenue_df, on="code", how="left").merge(fund_df, on="code", how="left")
    candidates["main_net_inflow"] = candidates["main_net_inflow"].fillna(0)
    candidates["lhb"] = candidates["code"].isin(lhb_codes)
    candidates = candidates[
        (candidates["pe"] > 0)
        & (candidates["pe"] < 50)
        & (candidates["volume_ratio"] > 1.5)
        & (candidates["main_net_inflow"] > 0)
        & (candidates["revenue_growth"].fillna(-999) > 10)
    ].copy()

    if lhb_codes:
        candidates = candidates[candidates["lhb"]].copy()

    if candidates.empty:
        return {
            "date": today_text(),
            "updated_at": now_cn().strftime("%Y-%m-%d %H:%M:%S"),
            "items": [],
            "warnings": warnings or ["按当前严格条件未筛出股票，可放宽龙虎榜或营收条件后再看。"],
        }

    seed = candidates.sort_values("main_net_inflow", ascending=False).head(80)
    rows: list[dict[str, Any]] = []
    for _, row in seed.iterrows():
        tech, tech_err = technical_check(row["code"])
        if tech_err:
            continue
        if not tech or not (tech["macd_cross"] and tech["ma_bull"]):
            continue
        rows.append(
            {
                "code": row["code"],
                "name": row["name"],
                "price": round(float(row["price"]), 2),
                "change_pct": round(float(row["change_pct"]), 2),
                "pe": round(float(row["pe"]), 2),
                "volume_ratio": round(float(row["volume_ratio"]), 2),
                "main_net_inflow": round(float(row["main_net_inflow"]), 2),
                "revenue_growth": round(float(row["revenue_growth"]), 2),
                "lhb": bool(row["lhb"]),
                **tech,
            }
        )
        if len(rows) >= 20:
            break

    reverse_key = "change_pct" if sort == "change" else "main_net_inflow"
    rows = sorted(rows, key=lambda item: item.get(reverse_key, 0), reverse=True)[:MAX_SCREEN_RESULTS]
    rows = add_ai_comments(rows)

    cache = load_cache()
    cache["last_recommendations"] = {"date": today_text(), "items": rows, "updated_at": now_cn().strftime("%Y-%m-%d %H:%M:%S")}
    save_cache(cache)
    return {"date": today_text(), "updated_at": now_cn().strftime("%Y-%m-%d %H:%M:%S"), "items": rows, "warnings": warnings}


def market_snapshot() -> dict[str, Any]:
    df, err = safe_fetch(ak.stock_zh_index_spot_em, timeout=15)
    if err or df is None or df.empty:
        return {"summary": "指数数据暂不可用", "warnings": [err or "指数数据为空"]}

    code_col = first_col(df, ["代码", "指数代码"])
    name_col = first_col(df, ["名称", "指数名称"])
    price_col = first_col(df, ["最新价", "收盘"])
    change_col = first_col(df, ["涨跌幅"])
    amount_col = first_col(df, ["成交额"])
    items = []
    for code in ["000001", "399001", "399006"]:
        hit = df[df[code_col].astype(str) == code] if code_col else pd.DataFrame()
        if hit.empty:
            continue
        row = hit.iloc[0]
        items.append(
            {
                "code": code,
                "name": str(row.get(name_col, "")),
                "price": to_number(row.get(price_col)),
                "change_pct": to_number(row.get(change_col)),
                "amount": to_number(row.get(amount_col)) if amount_col else None,
            }
        )
    summary = "；".join(f"{item['name']} {item['change_pct']}%" for item in items if item["change_pct"] is not None)
    return {"summary": summary or "指数数据暂不可用", "items": items, "warnings": []}


def build_after_hours(refresh: bool = False) -> dict[str, Any]:
    if now_cn().time() < time(15, 30) and not refresh:
        return {"available": False, "message": "盘后复盘 15:30 后可用。", "date": today_text()}

    cache = load_cache()
    recommendations = cache.get("last_recommendations", {})
    rec_items = recommendations.get("items", [])
    spot_df, err = safe_fetch(ak.stock_zh_a_spot_em, timeout=20)
    performance: list[dict[str, Any]] = []
    warnings: list[str] = []
    if err or spot_df is None or spot_df.empty:
        warnings.append(err or "实时行情为空")
    else:
        try:
            spot = normalize_spot(spot_df)
            for item in rec_items:
                hit = spot[spot["code"] == item["code"]]
                if hit.empty:
                    continue
                row = hit.iloc[0]
                performance.append(
                    {
                        "code": item["code"],
                        "name": item["name"],
                        "recommended_price": item.get("price"),
                        "close_price": round(float(row["price"]), 2),
                        "change_pct": round(float(row["change_pct"]), 2),
                    }
                )
        except Exception as exc:
            warnings.append(str(exc))

    market = market_snapshot()
    fallback = {
        "conclusion": "今日以复盘和观察为主，明日先看指数承接，不满足条件不出手。",
        "tomorrow_plan": "只做60或000主板，单票不超20%，总仓不超30%。",
        "risk": "市场波动较大，不构成投资建议。",
    }
    prompt = json.dumps(
        {
            "任务": "根据今日推荐股票表现和大盘走势，生成盘后复盘和明日操作建议。",
            "限制": "必须简洁；只允许分析60或000主板；输出JSON字段 conclusion,tomorrow_plan,risk。",
            "推荐股票表现": performance,
            "大盘": market,
        },
        ensure_ascii=False,
    )
    ai = call_openai_json(prompt, fallback)
    if not isinstance(ai, dict):
        ai = fallback

    result = {
        "available": True,
        "date": today_text(),
        "updated_at": now_cn().strftime("%Y-%m-%d %H:%M:%S"),
        "recommendation_date": recommendations.get("date"),
        "performance": performance,
        "market": market,
        "ai": ai,
        "warnings": warnings + market.get("warnings", []),
        "ai_enabled": has_openai_key(),
    }
    cache["after_hours"] = result
    save_cache(cache)
    return result


def scheduler_refresh_pre_market() -> None:
    with app.app_context():
        build_pre_market(refresh=True)


def scheduler_refresh_after_hours() -> None:
    with app.app_context():
        build_after_hours(refresh=True)


def start_scheduler() -> None:
    global scheduler_instance
    if os.getenv("ENABLE_SCHEDULER", "true").lower() != "true":
        return
    if scheduler_instance is not None:
        return
    scheduler = BackgroundScheduler(timezone=CN_TZ)
    scheduler.add_job(scheduler_refresh_pre_market, "cron", day_of_week="mon-fri", hour=8, minute=50, id="pre_market")
    scheduler.add_job(scheduler_refresh_after_hours, "cron", day_of_week="mon-fri", hour=15, minute=35, id="after_hours")
    scheduler.start()
    scheduler_instance = scheduler


@app.route("/")
def index():
    return render_template("index.html", ai_enabled=has_openai_key(), model=OPENAI_MODEL)


@app.route("/api/health")
def health():
    return jsonify({"ok": True, "time": now_cn().strftime("%Y-%m-%d %H:%M:%S"), "ai_enabled": has_openai_key()})


@app.route("/api/pre-market")
def api_pre_market():
    refresh = request.args.get("refresh") == "1"
    return jsonify(build_pre_market(refresh=refresh))


@app.route("/api/screen", methods=["POST"])
def api_screen():
    sort = request.json.get("sort", "main_flow") if request.is_json else request.form.get("sort", "main_flow")
    return jsonify(run_screen(sort=sort))


@app.route("/api/after-hours")
def api_after_hours():
    refresh = request.args.get("refresh") == "1"
    return jsonify(build_after_hours(refresh=refresh))


if mp.current_process().name == "MainProcess":
    start_scheduler()


if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port, debug=os.getenv("FLASK_DEBUG") == "1")
