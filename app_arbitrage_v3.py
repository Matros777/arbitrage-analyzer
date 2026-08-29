import os
import json
import statistics
import httpx
from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse
from typing import List, Dict, Any
from datetime import datetime

app = FastAPI(title="Arbitrage Analyzer Pro")
app.mount("/assets", StaticFiles(directory="assets", html=True), name="assets")

MIN_LIQUIDITY = 50000
MIN_VOLUME = 5000
MIN_PRICE = 0.00000001
MAX_PRICE_DEVIATION = 3.0  # drop pools whose price deviates >3x from median (clones/glitches)

def get_dex_data(symbol: str) -> Dict[str, Any]:
    try:
        with httpx.Client(timeout=15.0) as client:
            response = client.get(
                f"https://api.dexscreener.com/latest/dex/search?q={symbol}"
            )
            data = response.json()
            pairs = data.get("pairs")
            if not pairs:
                return {"error": f"Token {symbol} not found"}
            sym_u = symbol.upper()
            candidates = []
            all_token_addrs = set()
            for pair in pairs:
                base = pair.get("baseToken", {})
                quote = pair.get("quoteToken", {})
                bs = base.get("symbol", "").upper()
                qs = quote.get("symbol", "").upper()
                if bs != sym_u and qs != sym_u:
                    continue
                # record the address of the token that actually matches the symbol
                if bs == sym_u:
                    all_token_addrs.add(base.get("address", ""))
                    target_addr = base.get("address", "")
                else:
                    all_token_addrs.add(quote.get("address", ""))
                    target_addr = quote.get("address", "")
                price_usd = float(pair.get("priceUsd", 0) or 0)
                price_native = float(pair.get("priceNative", 0) or 0)
                liquidity = float(pair.get("liquidity", {}).get("usd", 0) or 0)
                volume = float(pair.get("volume", {}).get("h24", 0) or 0)
                if liquidity < MIN_LIQUIDITY or volume < MIN_VOLUME:
                    continue
                if bs == sym_u and qs == sym_u:
                    continue  # self pair
                # price of the TARGET token in USD (base or quote side)
                if bs == sym_u:
                    target_price = price_usd
                else:
                    if price_native > 0:
                        target_price = price_usd / price_native
                    else:
                        continue
                if target_price <= MIN_PRICE:
                    continue
                candidates.append({
                    "token_address": target_addr,
                    "dex_id": pair.get("dexId", "unknown"),
                    "target_price": target_price,
                    "liquidity": liquidity,
                    "volume": volume,
                    "price_change_1h": float(pair.get("priceChange", {}).get("h1", 0)),
                    "price_change_24h": float(pair.get("priceChange", {}).get("h24", 0)),
                    "base_token": base.get("symbol", ""),
                    "quote_token": quote.get("symbol", ""),
                    "pair_address": pair.get("pairAddress", ""),
                })
            if not candidates:
                return {"error": f"No valid pools found for {symbol}"}
            # canonical token = the address traded on the most DEXs/pools.
            # Real tokens have broad coverage; clones usually appear in only 1-2 pools,
            # so this picks the genuine token instead of a high-volume clone.
            addr_pools: Dict[str, list] = {}
            addr_vol: Dict[str, float] = {}
            for c in candidates:
                addr_pools.setdefault(c["token_address"], []).append(c)
                addr_vol[c["token_address"]] = addr_vol.get(c["token_address"], 0) + c["volume"]
            canonical_addr = max(
                addr_pools.keys(),
                key=lambda a: (len(addr_pools[a]), addr_vol[a]),
            )
            kept = addr_pools[canonical_addr]
            # median-based outlier filter: drops clones/glitches with a wild price
            prices = [c["target_price"] for c in kept]
            median_price = statistics.median(prices)
            deviation_flag = False
            if len(kept) >= 3:
                lo = median_price / MAX_PRICE_DEVIATION
                hi = median_price * MAX_PRICE_DEVIATION
                before = len(kept)
                kept = [c for c in kept if lo <= c["target_price"] <= hi]
                deviation_flag = len(kept) < before
            if not kept:
                return {"error": f"No valid pools found for {symbol}"}
            results = {}
            for c in kept:
                dex_id = c["dex_id"]
                results.setdefault(dex_id, []).append({
                    "price_usd": c["target_price"],
                    "liquidity_usd": c["liquidity"],
                    "volume_24h_usd": c["volume"],
                    "price_change_1h": c["price_change_1h"],
                    "price_change_24h": c["price_change_24h"],
                    "base_token": c["base_token"],
                    "quote_token": c["quote_token"],
                    "pair_address": c["pair_address"],
                    "token_address": c["token_address"],
                })
            for dex in results:
                results[dex].sort(key=lambda x: x["liquidity_usd"], reverse=True)
            validation = {
                "symbol": symbol,
                "canonical_address": canonical_addr,
                "distinct_token_addresses": sorted(a for a in all_token_addrs if a),
                "distinct_address_count": len([a for a in all_token_addrs if a]),
                "pools_considered": len(candidates),
                "pools_canonical": len(kept),
                "median_price": median_price,
                "price_deviation_filtered": deviation_flag,
                "note": "Prices compared only within the highest-volume token address. "
                        "Multiple distinct addresses sharing the same ticker indicate clones/scams.",
            }
            return {"pools": results, "validation": validation}
    except Exception as e:
        return {"error": f"Failed to fetch data: {str(e)}"}

def generate_explanation(metrics: Dict[str, Any], symbol: str) -> str:
    lines = []
    lines.append(f"📊 ANALYSIS FOR {symbol}")
    lines.append("=" * 60)
    total_liq = metrics["volume_stats"]["total_liquidity"]
    total_vol = metrics["volume_stats"]["total_volume"]
    avg_change_1h = metrics["volume_stats"]["avg_change_1h"]
    avg_change_24h = metrics["volume_stats"]["avg_change_24h"]
    lines.append(f"\n📈 OVERALL STATISTICS:")
    lines.append(f"  • Total liquidity: ${total_liq:,.0f} ({total_liq/1000000:.2f}M)")
    lines.append(f"  • 24h volume: ${total_vol:,.0f} ({total_vol/1000000:.2f}M)")
    lines.append(f"  • Avg 1h change: {avg_change_1h:+.2f}%")
    lines.append(f"  • Avg 24h change: {avg_change_24h:+.2f}%")
    spread = metrics["price_stats"]["spread_percent"]
    max_price = metrics["price_stats"]["max_price"]
    min_price = metrics["price_stats"]["min_price"]
    best_dex = metrics["price_stats"]["best_dex"]
    worst_dex = metrics["price_stats"]["worst_dex"]
    lines.append(f"\n💰 PRICE ANALYSIS:")
    lines.append(f"  • Spread between DEX: {spread:.2f}%")
    lines.append(f"  • Max price: ${max_price:.8f} ({best_dex})")
    lines.append(f"  • Min price: ${min_price:.8f} ({worst_dex})")
    lines.append(f"  • Price difference: ${max_price - min_price:.8f}")
    lines.append(f"\n🎯 RECOMMENDATIONS:")
    if spread > 2.0 and total_liq > 500000:
        lines.append(f"  ✅ Arbitrage opportunity found!")
        lines.append(f"     • Buy on {worst_dex} at ${min_price:.8f}")
        lines.append(f"     • Sell on {best_dex} at ${max_price:.8f}")
        lines.append(f"     • Potential margin: {spread:.2f}%")
        trade_size = min(1000, total_liq * 0.01)
        profit = (max_price - min_price) * trade_size
        lines.append(f"     • Estimated profit from ${trade_size:.0f}: ${profit:.2f}")
        if total_liq > 2000000:
            lines.append(f"     • Risk: LOW")
        elif total_liq > 500000:
            lines.append(f"     • Risk: MEDIUM")
        else:
            lines.append(f"     • Risk: HIGH")
    elif spread > 0.5:
        lines.append(f"  📊 Spread {spread:.2f}% — too small for arbitrage")
    else:
        lines.append(f"  📊 No spread — prices are identical")
    lines.append(f"\n📉 TREND ANALYSIS:")
    if avg_change_1h > 5:
        lines.append(f"  • Bullish trend: +{avg_change_1h:.2f}% in 1h")
    elif avg_change_1h < -5:
        lines.append(f"  • Bearish trend: {avg_change_1h:.2f}% in 1h")
    else:
        lines.append(f"  • Neutral trend: {avg_change_1h:+.2f}% in 1h")
    lines.append(f"\n💧 LIQUIDITY:")
    if total_liq > 2000000:
        lines.append(f"  • High liquidity (> $2M)")
    elif total_liq > 500000:
        lines.append(f"  • Medium liquidity ($500K - $2M)")
    else:
        lines.append(f"  • Low liquidity (< $500K)")
    lines.append(f"\n✅ FINAL VERDICT:")
    if spread > 2.0 and total_liq > 500000:
        lines.append(f"  • Arbitrage opportunity exists! You can enter.")
    elif spread > 0.5:
        lines.append(f"  • Arbitrage possible but margin is small ({spread:.2f}%)")
    else:
        lines.append(f"  • No arbitrage. Look for another token.")
    lines.append(f"\n🕐 Analysis: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("=" * 60)
    return "\n".join(lines)

def generate_explanation_ru(metrics: Dict[str, Any], symbol: str) -> str:
    lines = []
    lines.append(f"📊 АНАЛИЗ ТОКЕНА {symbol}")
    lines.append("=" * 60)
    total_liq = metrics["volume_stats"]["total_liquidity"]
    total_vol = metrics["volume_stats"]["total_volume"]
    avg_change_1h = metrics["volume_stats"]["avg_change_1h"]
    avg_change_24h = metrics["volume_stats"]["avg_change_24h"]
    lines.append(f"\n📈 ОБЩАЯ СТАТИСТИКА:")
    lines.append(f"  • Общая ликвидность: ${total_liq:,.0f} ({total_liq/1000000:.2f}M)")
    lines.append(f"  • Объём за 24ч: ${total_vol:,.0f} ({total_vol/1000000:.2f}M)")
    lines.append(f"  • Среднее изменение за 1ч: {avg_change_1h:+.2f}%")
    lines.append(f"  • Среднее изменение за 24ч: {avg_change_24h:+.2f}%")
    spread = metrics["price_stats"]["spread_percent"]
    max_price = metrics["price_stats"]["max_price"]
    min_price = metrics["price_stats"]["min_price"]
    best_dex = metrics["price_stats"]["best_dex"]
    worst_dex = metrics["price_stats"]["worst_dex"]
    lines.append(f"\n💰 ЦЕНОВОЙ АНАЛИЗ:")
    lines.append(f"  • Спред между DEX: {spread:.2f}%")
    lines.append(f"  • Максимальная цена: ${max_price:.8f} ({best_dex})")
    lines.append(f"  • Минимальная цена: ${min_price:.8f} ({worst_dex})")
    lines.append(f"  • Разница в цене: ${max_price - min_price:.8f}")
    lines.append(f"\n🎯 РЕКОМЕНДАЦИИ:")
    if spread > 2.0 and total_liq > 500000:
        lines.append(f"  ✅ Арбитражная возможность найдена!")
        lines.append(f"     • Покупай на {worst_dex} по ${min_price:.8f}")
        lines.append(f"     • Продавай на {best_dex} по ${max_price:.8f}")
        lines.append(f"     • Потенциальная маржа: {spread:.2f}%")
        trade_size = min(1000, total_liq * 0.01)
        profit = (max_price - min_price) * trade_size
        lines.append(f"     • Примерная прибыль с ${trade_size:.0f}: ${profit:.2f}")
        if total_liq > 2000000:
            lines.append(f"     • Риск: НИЗКИЙ")
        elif total_liq > 500000:
            lines.append(f"     • Риск: СРЕДНИЙ")
        else:
            lines.append(f"     • Риск: ВЫСОКИЙ")
    elif spread > 0.5:
        lines.append(f"  📊 Спред {spread:.2f}% — слишком мал для арбитража")
    else:
        lines.append(f"  📊 Спред отсутствует — цены одинаковые")
    lines.append(f"\n📉 ТРЕНДОВЫЙ АНАЛИЗ:")
    if avg_change_1h > 5:
        lines.append(f"  • Бычий тренд: +{avg_change_1h:.2f}% за 1ч")
    elif avg_change_1h < -5:
        lines.append(f"  • Медвежий тренд: {avg_change_1h:.2f}% за 1ч")
    else:
        lines.append(f"  • Нейтральный тренд: {avg_change_1h:+.2f}% за 1ч")
    lines.append(f"\n💧 ЛИКВИДНОСТЬ:")
    if total_liq > 2000000:
        lines.append(f"  • Высокая ликвидность (> $2M)")
    elif total_liq > 500000:
        lines.append(f"  • Средняя ликвидность ($500K - $2M)")
    else:
        lines.append(f"  • Низкая ликвидность (< $500K)")
    lines.append(f"\n✅ ИТОГОВЫЙ ВЕРДИКТ:")
    if spread > 2.0 and total_liq > 500000:
        lines.append(f"  • Арбитражная возможность есть! Можно входить.")
    elif spread > 0.5:
        lines.append(f"  • Арбитраж возможен, но маржа маленькая ({spread:.2f}%)")
    else:
        lines.append(f"  • Арбитража нет. Ищи другой токен.")
    lines.append(f"\n🕐 Анализ выполнен: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("=" * 60)
    return "\n".join(lines)

def calculate_arbitrage_metrics(dex_data: Dict[str, Any], symbol: str) -> Dict[str, Any]:
    if "error" in dex_data:
        return {"error": dex_data["error"]}
    pools = dex_data.get("pools", dex_data)
    all_pairs = []
    for dex, pairs in pools.items():
        for pair in pairs:
            all_pairs.append({
                "dex": dex,
                "price": pair["price_usd"],
                "liquidity": pair["liquidity_usd"],
                "volume": pair["volume_24h_usd"],
                "change_1h": pair["price_change_1h"],
                "change_24h": pair["price_change_24h"],
                "address": pair.get("pair_address", ""),
                "token_address": pair.get("token_address", ""),
                "quote": pair.get("quote_token", ""),
                "base": pair.get("base_token", ""),
            })
    if not all_pairs:
        return {"error": "No valid trading pairs found"}
    prices = [p["price"] for p in all_pairs if p["price"] > 0]
    liquidities = [p["liquidity"] for p in all_pairs]
    volumes = [p["volume"] for p in all_pairs]
    changes_1h = [p["change_1h"] for p in all_pairs]
    changes_24h = [p["change_24h"] for p in all_pairs]
    if not prices:
        return {"error": "No valid prices found"}
    max_price = max(prices)
    min_price = min(prices)
    avg_price = sum(prices) / len(prices)
    spread = max_price - min_price
    spread_percent = (spread / avg_price) * 100 if avg_price > 0 else 0
    best_dex = max(all_pairs, key=lambda x: x["price"])
    worst_dex = min(all_pairs, key=lambda x: x["price"])
    total_liquidity = sum(liquidities)
    total_volume = sum(volumes)
    avg_change_1h = sum(changes_1h) / len(changes_1h) if changes_1h else 0
    avg_change_24h = sum(changes_24h) / len(changes_24h) if changes_24h else 0
    recommendations = []
    if spread_percent > 2.0 and total_liquidity > 500000:
        recommendations.append({
            "type": "🚀 ARBITRAGE OPPORTUNITY",
            "message": f"Buy on {worst_dex['dex']} at ${worst_dex['price']:.8f}, Sell on {best_dex['dex']} at ${best_dex['price']:.8f}",
            "profit_margin": f"{spread_percent:.2f}%",
            "estimated_profit": f"${(spread * min(1000, total_liquidity * 0.01)):.2f}"
        })
    elif spread_percent > 0.5:
        recommendations.append({
            "type": "📊 SMALL SPREAD",
            "message": f"Small spread detected: {spread_percent:.2f}%. Monitor for better opportunities.",
            "profit_margin": f"{spread_percent:.2f}%",
        })
    if total_liquidity < 500000:
        recommendations.append({
            "type": "⚠️ LOW LIQUIDITY",
            "message": f"Total liquidity is low (${total_liquidity:,.0f}). High slippage risk.",
        })
    elif total_liquidity < 2000000:
        recommendations.append({
            "type": "⚠️ MEDIUM LIQUIDITY",
            "message": f"Total liquidity is moderate (${total_liquidity:,.0f}). Moderate slippage risk.",
        })
    else:
        recommendations.append({
            "type": "✅ HIGH LIQUIDITY",
            "message": f"Total liquidity is high (${total_liquidity:,.0f}). Low slippage risk.",
        })
    if avg_change_1h > 5:
        recommendations.append({
            "type": "📈 BULLISH TREND",
            "message": f"Strong upward trend: {avg_change_1h:.1f}% in 1h",
        })
    elif avg_change_1h < -5:
        recommendations.append({
            "type": "📉 BEARISH TREND",
            "message": f"Strong downward trend: {avg_change_1h:.1f}% in 1h",
        })
    metrics = {
        "price_stats": {
            "max_price": max_price,
            "min_price": min_price,
            "avg_price": avg_price,
            "spread": spread,
            "spread_percent": spread_percent,
            "best_dex": best_dex["dex"],
            "best_price": best_dex["price"],
            "worst_dex": worst_dex["dex"],
            "worst_price": worst_dex["price"],
        },
        "volume_stats": {
            "total_liquidity": total_liquidity,
            "total_volume": total_volume,
            "avg_change_1h": avg_change_1h,
            "avg_change_24h": avg_change_24h,
        },
        "recommendations": recommendations,
        "all_pairs": all_pairs
    }
    metrics["explanation_en"] = generate_explanation(metrics, symbol)
    metrics["explanation_ru"] = generate_explanation_ru(metrics, symbol)
    metrics["validation"] = dex_data.get("validation", {})
    return metrics

HTML_PAGE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Arbitrage Analyzer Pro — Multi-DEX Arbitrage Tool</title>
    <meta name="title" content="Arbitrage Analyzer Pro — Multi-DEX Arbitrage Tool">
    <meta name="description" content="Professional multi-DEX arbitrage analyzer with real-time price spread detection, liquidity analysis, and multi-DEX coverage.">
    <meta name="keywords" content="arbitrage, dex, crypto, trading, fastapi, dexscreener, memecoin, solana, ethereum, base">
    <meta name="robots" content="index, follow">
    <link rel="canonical" href="https://dex-arbitrage-pro.vercel.app/">
    <meta property="og:type" content="website">
    <meta property="og:url" content="https://dex-arbitrage-pro.vercel.app/">
    <meta property="og:title" content="Arbitrage Analyzer Pro — Multi-DEX Arbitrage Tool">
    <meta property="og:description" content="Professional multi-DEX arbitrage analyzer with real-time price spread detection, liquidity analysis, and multi-DEX coverage.">
    <meta property="og:image" content="https://dex-arbitrage-pro.vercel.app/assets/img/og-card.jpg">
    <meta property="og:site_name" content="Arbitrage Analyzer Pro">
    <meta property="og:locale" content="en_US">
    <meta property="twitter:card" content="summary_large_image">
    <meta property="twitter:url" content="https://dex-arbitrage-pro.vercel.app/">
    <meta property="twitter:title" content="Arbitrage Analyzer Pro — Multi-DEX Arbitrage Tool">
    <meta property="twitter:description" content="Professional multi-DEX arbitrage analyzer with real-time price spread detection, liquidity analysis, and multi-DEX coverage.">
    <meta property="twitter:image" content="https://dex-arbitrage-pro.vercel.app/assets/img/og-card.jpg">
    <link rel="icon" type="image/x-icon" href="/assets/icons/favicon.ico">
    <link rel="icon" type="image/png" sizes="16x16" href="/assets/icons/favicon-16x16.png">
    <link rel="icon" type="image/png" sizes="32x32" href="/assets/icons/favicon-32x32.png">
    <link rel="icon" type="image/png" sizes="192x192" href="/assets/icons/android-chrome-192x192.png">
    <link rel="icon" type="image/png" sizes="512x512" href="/assets/icons/android-chrome-512x512.png">
    <link rel="apple-touch-icon" sizes="180x180" href="/assets/icons/apple-touch-icon.png">
    <link rel="apple-touch-icon" sizes="512x512" href="/assets/icons/android-chrome-512x512.png">
    <link rel="manifest" href="/site.webmanifest">
    <meta name="theme-color" content="#0a0806">
    <meta property="og:image:width" content="1168">
    <meta property="og:image:height" content="784">
    <meta name="keywords" content="arbitrage, dex, crypto trading, solana, ethereum, base, memecoin, dexscreener, price spread, liquidity, on-chain analysis, arbitrage analyzer">
    <meta name="author" content="Arbitrage Analyzer Pro">
    <meta name="rating" content="general">
    <script type="application/ld+json">
    {
      "@context": "https://schema.org",
      "@graph": [
        {
          "@type": "WebSite",
          "@id": "https://dex-arbitrage-pro.vercel.app/#website",
          "url": "https://dex-arbitrage-pro.vercel.app/",
          "name": "Arbitrage Analyzer Pro",
          "description": "Professional multi-DEX arbitrage analyzer with real-time price spread detection, liquidity analysis, and multi-DEX coverage.",
          "inLanguage": "en"
        },
        {
          "@type": "SoftwareApplication",
          "name": "Arbitrage Analyzer Pro",
          "applicationCategory": "FinanceApplication",
          "operatingSystem": "Web",
          "url": "https://dex-arbitrage-pro.vercel.app/",
          "description": "Real-time multi-DEX arbitrage analysis: price spread detection, liquidity and volume analysis across Solana, Ethereum and Base in real time.",
          "offers": { "@type": "Offer", "price": "0", "priceCurrency": "USD" },
          "featureList": "Multi-DEX arbitrage, real-time price spreads, liquidity analysis, Solana/Ethereum/Base support, auto-update 5 min",
          "inLanguage": "en"
        }
      ]
    }
    </script>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;600;700;800;900&display=swap" rel="stylesheet">
    <style>
        * { margin:0; padding:0; box-sizing:border-box; }
        body {
            font-family: 'Inter', sans-serif;
            background: #0a0806;
            color: #f5f0eb;
            min-height: 100vh;
            background: radial-gradient(ellipse at 20% 10%, rgba(180,90,20,0.15), transparent 50%),
                        radial-gradient(ellipse at 80% 20%, rgba(140,50,10,0.1), transparent 55%),
                        #0a0806;
        }
        .container { max-width: 1280px; margin: 0 auto; padding: 24px; }
        .header { text-align: center; margin-bottom: 32px; }
        .header .icon { display: flex; justify-content: center; margin-bottom: 12px; }
        .header .icon img { width: 96px; height: 96px; object-fit: contain; border-radius: 16px; box-shadow: 0 0 24px rgba(245,200,66,0.25); }
        .header h1 {
            font-size: 38px;
            font-weight: 900;
            background: linear-gradient(135deg, #f5c842, #e09d1e);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
        }
        .header .subtitle {
            color: rgba(245,240,235,0.4);
            font-size: 14px;
            margin-top: 4px;
            letter-spacing: 1px;
        }
        .search-card {
            background: rgba(255,255,255,0.04);
            border: 1px solid rgba(255,255,255,0.08);
            border-radius: 16px;
            padding: 24px;
            margin-bottom: 24px;
            display: flex;
            gap: 12px;
            flex-wrap: wrap;
            align-items: center;
        }
        .search-card input {
            flex: 1;
            padding: 14px 20px;
            border-radius: 10px;
            border: 1px solid rgba(255,255,255,0.1);
            background: rgba(255,255,255,0.05);
            color: #f5f0eb;
            font-size: 16px;
            font-weight: 500;
            outline: none;
            transition: all 0.3s;
            min-width: 200px;
        }
        .search-card input:focus { border-color: #f5c842; box-shadow: 0 0 20px rgba(245,200,66,0.1); }
        .search-card input::placeholder { color: rgba(245,240,235,0.3); }
        .search-card button {
            padding: 14px 36px;
            border-radius: 10px;
            border: none;
            background: linear-gradient(135deg, #f5c842, #e09d1e);
            color: #0d0d0d;
            font-weight: 700;
            font-size: 16px;
            cursor: pointer;
            transition: all 0.3s;
        }
        .search-card button:hover { transform: translateY(-2px); box-shadow: 0 0 30px rgba(245,200,66,0.3); }
        .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; margin-top: 24px; }
        .card {
            background: rgba(255,255,255,0.03);
            border: 1px solid rgba(255,255,255,0.06);
            border-radius: 16px;
            padding: 20px;
        }
        .card h3 {
            font-size: 14px;
            text-transform: uppercase;
            letter-spacing: 1px;
            color: rgba(245,240,235,0.3);
            margin-bottom: 12px;
        }
        .card .value { font-size: 22px; font-weight: 700; }
        .card .value.green { color: #4ade80; }
        .card .value.red { color: #f87171; }
        .card .value.gold { color: #f5c842; }
        .card .label { font-size: 12px; color: rgba(245,240,235,0.3); margin-top: 4px; }
        .dex-table { width: 100%; border-collapse: collapse; font-size: 13px; }
        .dex-table th {
            text-align: left;
            padding: 10px 8px;
            color: rgba(245,240,235,0.3);
            font-weight: 600;
            border-bottom: 1px solid rgba(255,255,255,0.05);
        }
        .dex-table td { padding: 10px 8px; border-bottom: 1px solid rgba(255,255,255,0.03); }
        .dex-table .best { color: #4ade80; }
        .dex-table .worst { color: #f87171; }
        .recommendation {
            padding: 12px 16px;
            border-radius: 10px;
            margin-bottom: 8px;
            border-left: 3px solid #f5c842;
            background: rgba(255,255,255,0.02);
        }
        .recommendation .title { font-weight: 600; font-size: 14px; }
        .recommendation .desc { font-size: 13px; color: rgba(245,240,235,0.6); margin-top: 2px; }
        .loading { text-align: center; padding: 40px; color: rgba(245,240,235,0.4); }
        .spinner { display: inline-block; width: 30px; height: 30px; border: 3px solid rgba(245,200,66,0.15); border-top-color: #f5c842; border-radius: 50%; animation: spin 0.8s linear infinite; }
        @keyframes spin { to { transform: rotate(360deg); } }
        .error { color: #f87171; padding: 16px; background: rgba(248,113,113,0.08); border-radius: 10px; border: 1px solid rgba(248,113,113,0.15); }
        .filter-info {
            font-size: 12px;
            color: rgba(245,240,235,0.2);
            text-align: right;
            margin-top: 8px;
        }
        .explanation-box {
            background: rgba(255,255,255,0.02);
            border: 1px solid rgba(255,255,255,0.06);
            border-radius: 16px;
            padding: 24px;
            margin-top: 12px;
            white-space: pre-wrap;
            font-family: 'Courier New', monospace;
            font-size: 13px;
            line-height: 1.6;
            color: #d0c8c0;
            overflow-x: auto;
        }
        .lang-toggle {
            display: inline-flex;
            gap: 8px;
            background: rgba(255,255,255,0.04);
            border-radius: 8px;
            padding: 4px;
            border: 1px solid rgba(255,255,255,0.06);
        }
        .lang-toggle button {
            padding: 6px 14px;
            border: none;
            border-radius: 6px;
            cursor: pointer;
            font-weight: 600;
            font-size: 12px;
            transition: all 0.3s;
            background: transparent;
            color: rgba(245,240,235,0.4);
        }
        .lang-toggle button.active {
            background: linear-gradient(135deg, #f5c842, #e09d1e);
            color: #0d0d0d;
            box-shadow: 0 0 20px rgba(245,200,66,0.15);
        }
        .lang-toggle button:hover:not(.active) {
            color: rgba(245,240,235,0.8);
        }
        .explanation-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            flex-wrap: wrap;
            gap: 12px;
        }
        .auto-update-badge {
            display: inline-block;
            font-size: 11px;
            color: #4ade80;
            background: rgba(74,222,128,0.1);
            padding: 2px 10px;
            border-radius: 12px;
            border: 1px solid rgba(74,222,128,0.15);
            margin-left: 8px;
        }
        @media (max-width: 768px) { .grid { grid-template-columns: 1fr; } .search-card { flex-direction: column; } }
    </style>
</head>
<body>
    <div class="container">
        <section style="max-width:1280px;margin:0 auto;padding:0 24px 8px;">
            <h2 style="font-size:22px;font-weight:800;color:#f5c842;text-align:center;margin:0 auto 12px;">Live Multi-DEX Arbitrage Scanner</h2>
            <h2 style="font-size:15px;font-weight:500;color:rgba(245,240,235,0.6);text-align:center;max-width:760px;margin:0 auto 24px;line-height:1.5;">Track real-time price spreads and liquidity across Solana, Ethereum and Base DEXs. Enter a token symbol (e.g. SOL, PEPE, ETH) to get instant arbitrage metrics, volume and health.</h2>
        </section>
        <div class="header">
            <div class="icon"><img src="/assets/icons/android-chrome-512x512.png" alt="Arbitrage Analyzer Pro logo" width="96" height="96"></div>
            <h1>Professional Multi-DEX Analysis &amp; Arbitrage Detection</h1>
            <div class="subtitle">Professional Multi-DEX Analysis & Arbitrage Detection</div>
        </div>

        <div class="search-card">
            <input type="text" id="symbolInput" placeholder="Enter token symbol: PEPE, DOGE, SHIB...">
            <button id="analyzeBtn">🔍 Analyze</button>
        </div>

        <div id="loading" class="loading" style="display:none;"><div class="spinner"></div> Loading data from all DEX...</div>
        <div id="error" class="error" style="display:none;"></div>
        <div id="results" style="display:none;"></div>
        <div class="filter-info">✓ Filtered: liquidity > $50K, volume > $5K, price > 0</div>

        <div class="scam-safety" style="margin:18px 0 32px;">
            <div class="scam-safety-header" style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;">
                <span class="scam-safety-title" style="font-weight:800;color:#fbbf24;font-size:15px;">⚠️ Scam Safety</span>
                <div class="lang-toggle">
                    <button data-lang="en" class="active" onclick="updateWarning('en')">🇬🇧 EN</button>
                    <button data-lang="ru" onclick="updateWarning('ru')">🇷🇺 RU</button>
                </div>
            </div>
            <div class="scam-safety-box" id="warningBox" style="margin-top:14px;padding:14px;background:rgba(255,180,0,0.05);border:1px solid rgba(251,191,36,0.35);border-radius:10px;font-size:13px;line-height:1.6;color:#e8e0d8;font-family:'Courier New', monospace;"></div>
        </div>
    </div>

    <script>
        const input = document.getElementById('symbolInput');
        const btn = document.getElementById('analyzeBtn');
        const resultsDiv = document.getElementById('results');
        const loadingDiv = document.getElementById('loading');
        const errorDiv = document.getElementById('error');

        let explanationEn = '';
        let explanationRu = '';
        let poolGuideEn = '';
        let poolGuideRu = '';
        let currentSymbol = '';
        let updateInterval = null;
        const UPDATE_INTERVAL_MS = 300000;

        function showLoading() { loadingDiv.style.display = 'block'; resultsDiv.style.display = 'none'; errorDiv.style.display = 'none'; }
        function hideLoading() { loadingDiv.style.display = 'none'; }
        function showError(msg) { errorDiv.textContent = msg; errorDiv.style.display = 'block'; resultsDiv.style.display = 'none'; }
        function showResults(html) { resultsDiv.innerHTML = html; resultsDiv.style.display = 'block'; errorDiv.style.display = 'none'; }

        function buildPoolGuide(lang, data) {
            const pairs = (data && data.all_pairs) || [];
            const buyAddr = pairs.length ? pairs.reduce((a,b)=>a.price<b.price?a:b).address : '—';
            const sellAddr = pairs.length ? pairs.reduce((a,b)=>a.price>b.price?a:b).address : '—';
            if (lang === 'ru') {
                return `<strong>🔎 Как найти и совершить сделку:</strong> у каждой пары в колонке <em>Pool</em> показан адрес пула. Скопируй его и вставь в поиск на DexScreener (dexscreener.com) или в кошелёк/агрегатор, чтобы увидеть этот пул и обменять токен. На одной бирже монета бывает на <strong>разных пулах</strong> с разной ценой — именно поэтому важно выбрать конкретный пул (Buy = дешёвый, Sell = дорогой).` +
                    `<ul style="margin:6px 0 0 18px;padding:0">` +
                    `<li><strong>Buy (купить)</strong> — пул с минимальной ценой: ${buyAddr}</li>` +
                    `<li><strong>Sell (продать)</strong> — пул с максимальной ценой: ${sellAddr}</li>` +
                    `</ul>`;
            }
            return `<strong>🔎 How to find and execute a trade:</strong> each pair shows the pool address in the <em>Pool</em> column. Copy it and paste into the search on DexScreener (dexscreener.com) or into your wallet/aggregator to view this pool and swap the token. The same token can trade on <strong>different pools</strong> with different prices on a single exchange — that is exactly why it is important to pick the specific pool (Buy = cheaper, Sell = more expensive).` +
                `<ul style="margin:6px 0 0 18px;padding:0">` +
                `<li><strong>Buy</strong> — pool with the lowest price: ${buyAddr}</li>` +
                `<li><strong>Sell</strong> — pool with the highest price: ${sellAddr}</li>` +
                `</ul>`;
        }

        function updateExplanation(lang) {
            const box = document.getElementById('explanationBox');
            const pg = document.getElementById('poolGuideBox');
            const btns = document.querySelectorAll('.explanation-header .lang-toggle button');
            btns.forEach(b => b.classList.remove('active'));
            if (lang === 'en') {
                if (box) box.textContent = explanationEn;
                if (pg) pg.innerHTML = poolGuideEn;
                document.querySelector('.explanation-header .lang-toggle button[data-lang="en"]')?.classList.add('active');
            } else {
                if (box) box.textContent = explanationRu;
                if (pg) pg.innerHTML = poolGuideRu;
                document.querySelector('.explanation-header .lang-toggle button[data-lang="ru"]')?.classList.add('active');
            }
        }

        const warningEn = `⚠️ <b>SCAM WARNING — always verify the token contract before trading.</b><br><br>
Our scanner matches tokens by ticker (e.g. "WBTC"), but <b>anyone can deploy a fake token with the same ticker</b>. Before swapping, open the pool and confirm the token's <b>contract address</b> against the official project source or a trusted explorer (DexScreener / CoinGecko). Never trade an address you have not verified.<br><br>
<b>How this scanner protects you:</b><br>
<ul style="margin:6px 0 0 18px;padding:0;">
<li><b>Single-asset comparison</b> — prices are compared only within one canonical token contract (the one traded across the most pools/DEXs). Different contracts sharing the same ticker are never cross-compared, so clone tokens cannot fabricate an arbitrage.</li>
<li><b>Outlier filtering</b> — any pool whose price deviates more than 3× from the median is dropped, removing broken or manipulated prices.</li>
<li><b>Transparency</b> — the API exposes <code>distinct_address_count</code> (how many different contracts hide behind the ticker) and the canonical address, so you can see whether clones are present.</li>
</ul><br>
This is a screening aid, <b>not financial advice</b>. Do your own verification.`;

        const warningRu = `⚠️ <b>ВНИМАНИЕ О СКАМАХ — всегда проверяйте адрес контракта токена перед сделкой.</b><br><br>
Наш сканер ищет токены по тикеру (например, «WBTC»), но <b>любой может выпустить фейковый токен с тем же тикером</b>. Перед свопом откройте пул и сверьте <b>адрес контракта</b> токена с официальным источником проекта или надёжным обозревателем (DexScreener / CoinGecko). Не торгуйте адресом, который не проверили.<br><br>
<b>Как сканер вас страхует:</b><br>
<ul style="margin:6px 0 0 18px;padding:0;">
<li><b>Сравнение в рамках одного актива</b> — цены сравниваются только внутри одного канонического контракта токена (того, что торгуется на бóльшем числе пулов/DEX). Разные контракты с одним тикером никогда не сравниваются между собой, поэтому клоны не могут создать ложный арбитраж.</li>
<li><b>Фильтр выбросов</b> — любой пул, цена которого отклоняется более чем в 3 раза от медианной, отбрасывается, убирая сломанные или манипулированные цены.</li>
<li><b>Прозрачность</b> — ответ API показывает <code>distinct_address_count</code> (сколько разных контрактов прячется за тикером) и канонический адрес, чтобы вы видели, есть ли клоны.</li>
</ul><br>
Это вспомогательный фильтр, <b>не финансовый совет</b>. Проверяйте всё сами.`;

        function updateWarning(lang) {
            const box = document.getElementById('warningBox');
            if (!box) return;
            const btns = document.querySelectorAll('.scam-safety .lang-toggle button');
            btns.forEach(b => b.classList.remove('active'));
            if (lang === 'en') {
                box.innerHTML = warningEn;
                document.querySelector('.scam-safety .lang-toggle button[data-lang="en"]')?.classList.add('active');
            } else {
                box.innerHTML = warningRu;
                document.querySelector('.scam-safety .lang-toggle button[data-lang="ru"]')?.classList.add('active');
            }
        }

        async function analyze() {
            const symbol = input.value.trim().toUpperCase();
            if (!symbol) {
                showError('Please enter a token symbol');
                return;
            }
            currentSymbol = symbol;
            showLoading();

            try {
                const res = await fetch(`/api/arbitrage?symbol=${encodeURIComponent(symbol)}`);
                const data = await res.json();
                hideLoading();

                if (data.error) {
                    showError(data.error);
                    return;
                }

                explanationEn = data.explanation_en || '';
                explanationRu = data.explanation_ru || '';
                poolGuideEn = buildPoolGuide('en', data);
                poolGuideRu = buildPoolGuide('ru', data);

                let html = '<div class="grid">';

                const stats = data.price_stats;
                html += `
                    <div class="card">
                        <h3>💰 Price Spread</h3>
                        <div class="value gold">${stats.spread_percent.toFixed(2)}%</div>
                        <div class="label">Max: $${stats.max_price.toFixed(8)} | Min: $${stats.min_price.toFixed(8)}</div>
                        <div class="label">Best DEX: <span class="best">${stats.best_dex}</span> ($${stats.best_price.toFixed(8)})</div>
                        <div class="label">Worst DEX: <span class="worst">${stats.worst_dex}</span> ($${stats.worst_price.toFixed(8)})</div>
                        <div class="label" style="color:#4ade80;font-size:11px;">🔄 Auto-update</div>
                    </div>
                `;

                const vol = data.volume_stats;
                html += `
                    <div class="card">
                        <h3>📊 Liquidity & Volume</h3>
                        <div class="value">$${(vol.total_liquidity/1000000).toFixed(2)}M</div>
                        <div class="label">Total liquidity across all DEX</div>
                        <div class="value" style="font-size:18px;margin-top:8px;">$${(vol.total_volume/1000000).toFixed(2)}M</div>
                        <div class="label">Total 24h volume</div>
                        <div class="label">1h: ${vol.avg_change_1h > 0 ? '+' : ''}${vol.avg_change_1h.toFixed(2)}% | 24h: ${vol.avg_change_24h > 0 ? '+' : ''}${vol.avg_change_24h.toFixed(2)}%</div>
                    </div>
                `;

                html += `
                    <div class="card" style="grid-column: 1 / -1;">
                        <h3>🎯 Arbitrage Recommendations</h3>
                        ${data.recommendations.map(r => `
                            <div class="recommendation">
                                <div class="title">${r.type}</div>
                                <div class="desc">${r.message}</div>
                                ${r.profit_margin ? `<div class="desc" style="color:#f5c842;">Margin: ${r.profit_margin}</div>` : ''}
                                ${r.estimated_profit ? `<div class="desc" style="color:#4ade80;">Estimated Profit: ${r.estimated_profit}</div>` : ''}
                            </div>
                        `).join('')}
                    </div>
                `;

                html += `
                    <div class="card" style="grid-column: 1 / -1;">
                        <h3>📋 DEX Comparison (clean pools)</h3>
                        <table class="dex-table">
                            <tr>
                                <th>DEX</th>
                                <th>Price</th>
                                <th>Liquidity</th>
                                <th>24h Volume</th>
                                <th>1h %</th>
                                <th>Pool (address)</th>
                            </tr>
                            ${data.all_pairs.map(p => `
                                <tr>
                                    <td><strong>${p.dex}</strong></td>
                                    <td>$${p.price.toFixed(8)}</td>
                                    <td>$${(p.liquidity/1000).toFixed(0)}K</td>
                                    <td>$${(p.volume/1000).toFixed(0)}K</td>
                                    <td class="${p.change_1h > 0 ? 'best' : 'worst'}">${p.change_1h > 0 ? '+' : ''}${p.change_1h.toFixed(2)}%</td>
                                    <td style="font-family:monospace;font-size:11px;word-break:break-all;max-width:190px;"><a href="https://dexscreener.com/search?q=${p.address}" target="_blank" rel="noopener" title="${p.address}" style="color:#7dd3fc;text-decoration:none;">${p.address ? p.address.slice(0,16) + '…' : (p.dex + ' pool')}</a></td>
                                </tr>
                            `).join('')}
                        </table>
                        </div>
                `;

                if (explanationEn || explanationRu) {
                    html += `
                        <div class="card" style="grid-column: 1 / -1;">
                            <div class="explanation-header">
                                <h3>📖 ANALYSIS & EXPLANATION</h3>
                                <div class="lang-toggle">
                                    <button data-lang="en" class="active" onclick="updateExplanation('en')">🇬🇧 EN</button>
                                    <button data-lang="ru" onclick="updateExplanation('ru')">🇷🇺 RU</button>
                                </div>
                            </div>
                            <div class="explanation-box" id="explanationBox">${explanationEn}</div>
                            <div class="pool-guide" id="poolGuideBox" style="margin-top:14px;padding:12px;background:rgba(255,255,255,0.03);border:1px solid rgba(255,255,255,0.08);border-radius:10px;font-size:13px;line-height:1.6;color:#d0c8c0;font-family:'Courier New', monospace;">${poolGuideEn}</div>
                        </div>
                    `;
                }

                html += '</div>';
                showResults(html);
                startAutoUpdate();

            } catch (e) {
                hideLoading();
                showError('Connection error');
            }
        }

        function startAutoUpdate() {
            if (updateInterval) {
                clearInterval(updateInterval);
                updateInterval = null;
            }
            updateInterval = setInterval(() => {
                if (currentSymbol) {
                    console.log(`🔄 Auto-updating ${currentSymbol}...`);
                    fetch(`/api/arbitrage?symbol=${encodeURIComponent(currentSymbol)}`)
                        .then(res => res.json())
                        .then(data => {
                            if (data.error) {
                                console.warn('Auto-update error:', data.error);
                                return;
                            }
                            explanationEn = data.explanation_en || '';
                            explanationRu = data.explanation_ru || '';
                            poolGuideEn = buildPoolGuide('en', data);
                            poolGuideRu = buildPoolGuide('ru', data);
                            updateExplanation(document.querySelector('.lang-toggle button.active')?.dataset.lang || 'en');
                            const stats = data.price_stats;
                            const priceCards = document.querySelectorAll('.card .value.gold');
                            if (priceCards.length > 0) {
                                priceCards[0].textContent = stats.spread_percent.toFixed(2) + '%';
                            }
                            const labels = document.querySelectorAll('.card .label');
                            if (labels.length > 0) {
                                const labelParts = labels[0].textContent.split('|');
                                if (labelParts.length === 2) {
                                    labels[0].textContent = 'Max: $' + stats.max_price.toFixed(8) + ' | Min: $' + stats.min_price.toFixed(8);
                                }
                            }
                            console.log('✅ Auto-update completed');
                        })
                        .catch(err => console.warn('Auto-update error:', err));
                }
            }, UPDATE_INTERVAL_MS);
        }

        btn.addEventListener('click', analyze);
        input.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') analyze();
        });
        updateWarning('en');
    </script>
</body>
</html>
"""

@app.get("/", response_class=HTMLResponse)
async def root():
    return HTMLResponse(HTML_PAGE)

@app.get("/site.webmanifest")
async def webmanifest():
    from fastapi.responses import FileResponse, PlainTextResponse
    import os as _os
    path = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), 'site.webmanifest')
    if not _os.path.exists(path):
        return JSONResponse({'error': 'manifest not found'}, status_code=404)
    return FileResponse(path, media_type='application/manifest+json')

@app.get("/favicon.ico")
async def favicon_ico():
    from fastapi.responses import FileResponse
    import os as _os
    path = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), 'assets', 'icons', 'favicon.ico')
    if not _os.path.exists(path):
        return JSONResponse({'error': 'favicon not found'}, status_code=404)
    return FileResponse(path, media_type='image/x-icon')

@app.get("/sitemap.xml")
async def sitemap():
    from fastapi.responses import Response
    base = "https://dex-arbitrage-pro.vercel.app"
    xml = ('<?xml version="1.0" encoding="UTF-8"?>\n'
           '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
           '  <url><loc>' + base + '/</loc><lastmod>2026-08-28</lastmod>'
           '<changefreq>daily</changefreq><priority>1.0</priority></url>\n'
           '</urlset>')
    return Response(content=xml, media_type='application/xml')

@app.get("/robots.txt")
async def robots():
    from fastapi.responses import PlainTextResponse
    txt = "User-agent: *\nAllow: /\nSitemap: https://dex-arbitrage-pro.vercel.app/sitemap.xml\n"
    return PlainTextResponse(txt)

@app.get("/api/arbitrage")
async def arbitrage_analysis(symbol: str):
    dex_data = get_dex_data(symbol)
    if "error" in dex_data:
        return dex_data
    result = calculate_arbitrage_metrics(dex_data, symbol)
    return result

@app.get("/health")
async def health():
    return {"status": "ok", "service": "Arbitrage Analyzer Pro"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8090)))


# Vercel Python Functions: экспорт ASGI-объекта
handler = app

