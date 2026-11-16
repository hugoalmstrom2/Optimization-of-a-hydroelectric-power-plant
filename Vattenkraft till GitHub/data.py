"""Riktiga indata: spot-, obalans- och stödtjänstpriser och tillrinning från SMHI.

Spotpriser (day-ahead, €/MWh) hämtas per dygn från elprisetjustnu.se, som publicerar
Nord Pools priser fritt från november 2022. Äldre priser, tillbaka till elområdesindelningen
i november 2011, kommer från Energi Data Service (Energinet). Sedan 1 oktober 2025 har
day-ahead-marknaden 15-minutersupplösning; äldre timpriser lagras som fyra lika kvartar.
Obalanspriser (eSett, gemensamt nordiskt avräkningsföretag) används för att avräkna
avvikelser mot budet. Kapacitetspriser och upphandlade volymer för FCR-N, FCR-D och
mFRR kommer från Svenska kraftnäts Mimer.

Tillrinningen bygger på SMHI:s station 2258 Idre 3 (2 384 km²) i Österdalälven, uppströms
Trängslet. Den är oreglerad och har fjällregim med vårflod i maj–juni. Den specifika
avrinningen (m³/s per km²) skalas med arean till varje kraftverks lokala avrinningsområde och
fördröjs med en linjär magasinsmodell, eftersom större områden med sjöar svarar långsammare.
Areorna är kalibrerade mot SMHI:s station 2436 vid Spjutmo kraftverk, som mäter det reglerade
flödet: över många år är det lika med den naturliga tillrinningen, eftersom magasinen går
runt. Samma station visar hur Fortum faktiskt har tappat.

För att jämföra med verklig drift kan faktisk produktion per aggregat hämtas från ENTSO-E:s
transparensplattform. Den publiceras för aggregat från 100 MW och per kontrollområde (hela
Sverige), inte per elområde. Det kräver en gratis API-nyckel i miljövariabeln ENTSOE_API_KEY
eller i filen data_cache/entsoe_token.txt.

Allt mellanlagras i data_cache/, så att senare körningar fungerar utan nät.
Tiden anges i CET (UTC+1) året runt, så att varje dygn har 96 kvartar.
"""
from __future__ import annotations

import io
import json
import os
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd

from cascade import Cascade

CACHE = Path(__file__).parent / "data_cache"
PRICE_URL = "https://www.elprisetjustnu.se/api/v1/prices/{d:%Y}/{d:%m-%d}_{area}.json"
IMBALANCE_URL = ("https://api.opendata.esett.com/EXP14/Prices?start={start:%Y-%m-%dT%H:%M:%S}.000Z"
                 "&end={end:%Y-%m-%dT%H:%M:%S}.000Z&mba={mba}")
SMHI_URL = ("https://opendata-download-hydroobs.smhi.se/api/version/latest/parameter/1/"
            "station/{station}/period/corrected-archive/data.csv")
HISTORY_URL = "https://api.energidataservice.dk/dataset/Elspotprices?{query}"
MIMER_PERIOD = ("periodFrom={start:%m}%2F{start:%d}%2F{start:%Y}%2000%3A00%3A00"
                "&periodTo={end:%m}%2F{end:%d}%2F{end:%Y}%2000%3A00%3A00")
MIMER_FCR_URL = "https://mimer.svk.se/PrimaryRegulation/DownloadText?{period}&auctionTypeId=1&productTypeId=0"
MIMER_MFRR_URL = "https://mimer.svk.se/ManualFrequencyRestorationReserveCM/DownloadText?{period}&ConstraintAreaId=0"
ENTSOE_URL = "https://web-api.tp.entsoe.eu/api?{query}"
HISTORY_START = pd.Timestamp("2011-11-01")        # elområdesindelningen infördes 1 november 2011
MBA_CODES = {"SE1": "10Y1001A1001A44P", "SE2": "10Y1001A1001A45N",
             "SE3": "10Y1001A1001A46L", "SE4": "10Y1001A1001A47J"}
CONTROL_AREAS = {"SE1": "10YSE-1--------K", "SE2": "10YSE-1--------K",
                 "SE3": "10YSE-1--------K", "SE4": "10YSE-1--------K"}
RUNOFF_STATION = 2258           # Idre 3, Österdalälven
RUNOFF_STATION_KM2 = 2383.92
REGULATED_STATION = 2436        # Spjutmo kraftverk, reglerat flöde
PRICE_HISTORY_START = pd.Timestamp("2022-11-01")  # första dygnet i det öppna pris-API:t
QUARTERS_FROM = pd.Timestamp("2025-10-01")        # day-ahead i 15-minutersupplösning
CET = "Etc/GMT-1"               # fast UTC+1 (tecknet är omvänt i Etc-zonerna)
QUARTER = pd.Timedelta(minutes=15)


def _download(url: str) -> bytes | None:
    """Hämtar en URL. Returnerar None om resursen inte finns (t.ex. morgondagens priser före kl 13)
    eller om nätet inte går att nå; då används det som redan finns sparat."""
    request = urllib.request.Request(url, headers={"User-Agent": "vattenkraft-planering/3.0"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.read()
    except urllib.error.HTTPError as err:
        if err.code == 404:
            return None
        raise
    except (urllib.error.URLError, OSError):
        return None


def _price_day(day: pd.Timestamp, area: str) -> list[dict]:
    """Ett dygns priser (tom lista om de inte är publicerade än). Publicerade priser ändras inte
    och mellanlagras därför direkt."""
    path = CACHE / f"priser_{area}" / f"{day:%Y-%m-%d}.json"
    if path.exists():
        return json.loads(path.read_text())
    raw = _download(PRICE_URL.format(d=day, area=area))
    time.sleep(0.05)  # var snäll mot det öppna API:t
    if raw is None:
        return []
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return json.loads(raw)


def _quarters(entries: list[dict]) -> pd.Series:
    """API-poster (15 min eller timme, lokal tid) till kvartspriser i CET. En timpost
    blir fyra kvartar med samma pris."""
    start = pd.to_datetime([e["time_start"] for e in entries], utc=True)
    end = pd.to_datetime([e["time_end"] for e in entries], utc=True)
    n = ((end - start) / QUARTER).astype(int)
    times = pd.DatetimeIndex([s + i * QUARTER for s, k in zip(start, n) for i in range(k)])
    values = np.repeat([e["EUR_per_kWh"] * 1000 for e in entries], n)
    return pd.Series(values, index=times.tz_convert(CET).tz_localize(None)).groupby(level=0).mean()


def _history_year(year: int, area: str) -> pd.Series:
    """Ett års timpriser (€/MWh, CET) från Energi Data Service, mellanlagrade per år. Tjänsten
    begränsar antalet anrop; då väntar vi så länge den anger och försöker igen."""
    path = CACHE / "prishistorik" / f"{area}_{year}.csv"
    if path.exists():
        return pd.read_csv(path, index_col=0, parse_dates=True).iloc[:, 0]
    query = urllib.parse.urlencode({"start": f"{year}-01-01", "end": f"{year + 1}-01-01", "limit": 0,
                                    "filter": json.dumps({"PriceArea": [area]}), "sort": "HourUTC asc"})
    for _ in range(20):
        try:
            raw = _download(HISTORY_URL.format(query=query))
            break
        except urllib.error.HTTPError as err:
            if err.code != 429:
                raise
            time.sleep(int(err.headers.get("Retry-After", 60)) + 2)
    else:
        raise RuntimeError(f"Energi Data Service svarar inte för {year}")
    records = [r for r in json.loads(raw or b"{}").get("records", []) if r["SpotPriceEUR"] is not None]
    times = pd.to_datetime([r["HourUTC"] for r in records]) + pd.Timedelta(hours=1)   # UTC -> CET
    prices = pd.Series([r["SpotPriceEUR"] for r in records], index=times, dtype=float).groupby(level=0).mean()
    if len(prices) > 8000 or (year == HISTORY_START.year and not prices.empty):
        path.parent.mkdir(parents=True, exist_ok=True)
        prices.rename("price").to_csv(path, float_format="%.4f")
    return prices


def _read_cache(path: Path) -> pd.Series:
    if not path.exists():
        return pd.Series(dtype=float, index=pd.DatetimeIndex([]))
    return pd.read_csv(path, index_col=0, parse_dates=True).iloc[:, 0]


def load_prices(start: pd.Timestamp, end: pd.Timestamp, area: str = "SE3", freq: str = "15min") -> pd.Series:
    """Spotpriser i €/MWh från start till end (CET, end exklusive), per kvart ("15min") eller
    timme ("h"). Ej publicerade perioder utelämnas. Kvartspriserna sparas i en fil per elområde
    och bara dygn som saknas där hämtas."""
    path = CACHE / f"kvartspriser_{area}.csv"
    cached = _read_cache(path)
    if cached.empty and (CACHE / f"timpriser_{area}.csv").exists():
        # Timpriser från före kvartsmarknaden är exakta som fyra lika kvartar.
        hourly = _read_cache(CACHE / f"timpriser_{area}.csv")
        hourly = hourly[hourly.index < QUARTERS_FROM]
        cached = hourly.reindex(pd.date_range(hourly.index[0], hourly.index[-1] + pd.Timedelta(minutes=45),
                                              freq=QUARTER)).ffill(limit=3)
    per_day = cached.groupby(cached.index.normalize()).size()
    missing = [d for d in pd.date_range(max(start, HISTORY_START).normalize(), (end - QUARTER).normalize())
               if per_day.get(d, 0) < 96]
    old = sorted({d.year for d in missing if d < PRICE_HISTORY_START})
    new = [d for d in missing if d >= PRICE_HISTORY_START]
    fresh = []
    hourly = pd.concat([_history_year(year, area) for year in old]) if old else pd.Series(dtype=float)
    if not hourly.empty:
        fresh.append(hourly.reindex(pd.date_range(hourly.index[0], hourly.index[-1] + pd.Timedelta(minutes=45),
                                                  freq=QUARTER)).ffill(limit=3).dropna())
    if new:
        # Ett CET-dygn D ligger i lokala dygn D och (sommartid) D+1.
        local_days = sorted({d + pd.Timedelta(days=i) for d in new for i in (0, 1)})
        entries = [e for day in local_days for e in _price_day(day, area)]
        if entries:
            fresh.append(_quarters(entries))
    if fresh:
        for series in fresh:
            cached = series.combine_first(cached).sort_index()
        CACHE.mkdir(exist_ok=True)
        cached.rename("price").to_csv(path, float_format="%.4f")
    prices = cached[(cached.index >= start) & (cached.index < end)].rename("price")
    if prices.empty:
        raise RuntimeError(f"Inga priser för {area} {start:%Y-%m-%d}–{end:%Y-%m-%d}")
    return prices.resample("h").mean().dropna() if freq == "h" else prices


def load_imbalance_prices(start: pd.Timestamp, end: pd.Timestamp, area: str = "SE3") -> pd.Series:
    """Obalanspris (€/MWh, enpris) per kvart från eSett, CET. Hämtas per månad och mellanlagras."""
    path = CACHE / f"obalanspriser_{area}.csv"
    cached = _read_cache(path)
    months = pd.date_range(start.normalize().replace(day=1), end, freq="MS")
    fetched = False
    for m in months:
        m_end = m + pd.offsets.MonthBegin(1)
        have = cached[(cached.index >= m) & (cached.index < m_end)]
        if len(have) >= (m_end - m) / QUARTER - 8 or m > pd.Timestamp.now():
            continue
        utc = lambda t: t - pd.Timedelta(hours=1)  # noqa: E731  CET -> UTC
        raw = _download(IMBALANCE_URL.format(start=utc(m), end=utc(m_end), mba=MBA_CODES[area]))
        rows = json.loads(raw or b"[]")
        if rows:
            times = pd.to_datetime([r["timestampUTC"] for r in rows], utc=True).tz_convert(CET).tz_localize(None)
            fresh = pd.Series([r["imblSalesPrice"] for r in rows], index=times, dtype=float)
            cached = fresh.combine_first(cached).sort_index()
            fetched = True
    if fetched:
        CACHE.mkdir(exist_ok=True)
        cached.rename("imbalance_price").to_csv(path, float_format="%.4f")
    return cached[(cached.index >= start) & (cached.index < end)].rename("imbalance_price")


RESERVES = ("fcr_n", "fcr_d_upp", "fcr_d_ned", "mfrr_upp", "mfrr_ned")


def _local_to_cet(times: pd.Series) -> pd.DatetimeIndex:
    """Svensk lokaltid till CET. Timmar som saknas eller dubbleras vid sommartidsbyte blir NaT."""
    local = pd.to_datetime(times).dt.tz_localize("Europe/Stockholm", ambiguous="NaT", nonexistent="NaT")
    return pd.DatetimeIndex(local.dt.tz_convert(CET).dt.tz_localize(None))


def _reserve_month(month: pd.Timestamp, area: str) -> pd.DataFrame:
    """En månads kapacitetspriser (€/MW per timme) och upphandlade volymer (MW) i area, per timme
    i CET. Hela månader mellanlagras."""
    path = CACHE / "stodtjanster" / f"{area}_{month:%Y-%m}.csv"
    if path.exists():
        return pd.read_csv(path, index_col=0, parse_dates=True)
    month_end = month + pd.offsets.MonthBegin(1)
    period = MIMER_PERIOD.format(start=month, end=month_end)
    columns = [f"pris_{r}" for r in RESERVES] + [f"volym_{r}" for r in RESERVES]
    fcr_raw, mfrr_raw = (_download(url.format(period=period)) for url in (MIMER_FCR_URL, MIMER_MFRR_URL))
    if not fcr_raw or not mfrr_raw:
        return pd.DataFrame(columns=columns, dtype=float)

    read = lambda raw: pd.read_csv(io.StringIO(raw.decode("utf-8-sig")), sep=";", decimal=",")  # noqa: E731
    fcr = read(fcr_raw)
    fcr = fcr[fcr["Datum"] != "Summa"]
    fcr.index = _local_to_cet(fcr["Datum"])
    mfrr = read(mfrr_raw)
    mfrr = mfrr[mfrr["Elområde"] == area.replace("SE", "SN")].drop_duplicates("Period", keep="last")
    mfrr.index = _local_to_cet(mfrr["Period"])
    out = pd.DataFrame({
        "pris_fcr_n": fcr["FCR-N Pris (EUR/MW)"], "pris_fcr_d_upp": fcr["FCR-D upp Pris (EUR/MW)"],
        "pris_fcr_d_ned": fcr["FCR-D ned Pris (EUR/MW)"],
        "volym_fcr_n": fcr[f"{area} FCRN"], "volym_fcr_d_upp": fcr[f"{area} FCRD upp"],
        "volym_fcr_d_ned": fcr[f"{area} FCRD ned"],
    }).join(pd.DataFrame({
        "pris_mfrr_upp": mfrr["mFRR Upp Pris (EUR/MW)"], "pris_mfrr_ned": mfrr["mFRR Ned Pris (EUR/MW)"],
        "volym_mfrr_upp": mfrr["mFRR Upp Volym (MW)"], "volym_mfrr_ned": mfrr["mFRR Ned Volym (MW)"],
    }), how="outer")[columns]
    out = out[out.index.notna()].astype(float)
    out = out[~out.index.duplicated()].sort_index()
    if month_end <= pd.Timestamp.now().normalize():
        path.parent.mkdir(parents=True, exist_ok=True)
        out.to_csv(path, float_format="%.4f")
    return out


def load_reserve_prices(start: pd.Timestamp, end: pd.Timestamp, area: str = "SE3") -> pd.DataFrame:
    """Kapacitetspriser (pris_*, €/MW per timme) och upphandlade volymer (volym_*, MW) per timme
    för FCR-N, FCR-D upp/ned och mFRR upp/ned, från start till end (CET, end exklusive)."""
    months = pd.date_range(start.normalize().replace(day=1), end, freq="MS")
    data = pd.concat([_reserve_month(m, area) for m in months])
    hours = pd.date_range(start, end, freq="h", inclusive="left")
    return data[~data.index.duplicated()].sort_index().reindex(hours).ffill().fillna(0.0)


def entsoe_token() -> str | None:
    token = os.environ.get("ENTSOE_API_KEY", "").strip()
    path = CACHE / "entsoe_token.txt"
    if not token and path.exists():
        token = path.read_text(encoding="utf-8").strip()
    return token or None


def parse_unit_generation(raw: bytes) -> pd.DataFrame:
    """Faktisk produktion i MW per aggregat ur ett svar från ENTSO-E (dokumenttyp A73), en
    kolumn per aggregat och tid i CET. Svaret kan vara XML eller en zip med XML-filer."""
    if raw[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            parts = [parse_unit_generation(archive.read(name)) for name in archive.namelist()]
        return pd.concat(parts, axis=1) if parts else pd.DataFrame()

    def children(node, tag):
        return [c for c in node if c.tag.rsplit("}", 1)[-1] == tag]

    def text(node, *path):
        for tag in path:
            node = children(node, tag)[0]
        return node.text

    series: dict[str, list[pd.Series]] = {}
    for ts in children(ET.fromstring(raw), "TimeSeries"):
        name = text(ts, "MktPSRType", "PowerSystemResources", "name")
        for period in children(ts, "Period"):
            start = pd.Timestamp(text(period, "timeInterval", "start"))
            end = pd.Timestamp(text(period, "timeInterval", "end"))
            step = pd.Timedelta(text(period, "resolution"))
            values = np.full(int((end - start) / step), np.nan)
            for point in children(period, "Point"):
                values[int(text(point, "position")) - 1] = float(text(point, "quantity"))
            # Kurvtyp A03: en utelämnad punkt har samma värde som den närmast före.
            times = pd.date_range(start, periods=len(values), freq=step).tz_convert(CET).tz_localize(None)
            series.setdefault(name, []).append(pd.Series(values, index=times).ffill())
    return pd.DataFrame({name: pd.concat(parts).sort_index() for name, parts in series.items()})


def _plain(name: str) -> str:
    """Gemener utan diakritiska tecken, så att "TRÄNGSLET G1" och "Trangslet" kan jämföras."""
    return unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()


def load_unit_generation(start: pd.Timestamp, end: pd.Timestamp, plant: str, area: str = "SE3") -> pd.DataFrame:
    """Faktisk produktion (MW per kvart, CET) för aggregaten vars namn innehåller plant, från
    ENTSO-E. Hämtas för hela kontrollområdet ett dygn i taget (gränsen för den här
    dokumenttypen) och mellanlagras."""
    token = entsoe_token()
    days = []
    for day in pd.date_range(start.normalize(), end - QUARTER, freq="D"):
        path = CACHE / "entsoe" / f"{area[:2]}_{day:%Y-%m-%d}.csv"
        if path.exists():
            days.append(pd.read_csv(path, index_col=0, parse_dates=True))
            continue
        if token is None:
            raise RuntimeError("Ingen API-nyckel till ENTSO-E: sätt ENTSOE_API_KEY eller skriv den i "
                               "data_cache/entsoe_token.txt")
        utc = day - pd.Timedelta(hours=1)
        query = urllib.parse.urlencode({"securityToken": token, "documentType": "A73", "processType": "A16",
                                        "in_Domain": CONTROL_AREAS[area], "periodStart": f"{utc:%Y%m%d%H%M}",
                                        "periodEnd": f"{utc + pd.Timedelta(days=1):%Y%m%d%H%M}"})
        raw = _download(ENTSOE_URL.format(query=query))
        time.sleep(0.2)
        frame = parse_unit_generation(raw) if raw else pd.DataFrame()
        if not frame.empty and day + pd.Timedelta(days=1) <= pd.Timestamp.now().normalize():
            path.parent.mkdir(parents=True, exist_ok=True)
            frame.to_csv(path, float_format="%.2f")
        days.append(frame)
    data = pd.concat(days).sort_index() if days else pd.DataFrame()
    quarters = pd.date_range(start, end, freq=QUARTER, inclusive="left")
    # Aggregaten kan rapportera med olika upplösning; varje aggregat fylls ut till kvartar för sig.
    return pd.DataFrame({c: data[c].dropna().groupby(level=0).last().reindex(quarters, method="ffill", limit=3)
                         for c in data.columns if _plain(plant) in _plain(c)}, index=quarters)


def _smhi_daily_flow(station: int, max_age_hours: float) -> pd.Series:
    """Vattenföring (m³/s) per dygn för en SMHI-station, mellanlagrad i max_age_hours."""
    path = CACHE / f"smhi_{station}.csv"
    if not path.exists() or time.time() - path.stat().st_mtime > max_age_hours * 3600:
        raw = _download(SMHI_URL.format(station=station))
        if raw:
            CACHE.mkdir(exist_ok=True)
            path.write_bytes(raw)
        elif not path.exists():
            raise RuntimeError(f"Kunde inte hämta SMHI-station {station} och inget finns sparat")
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    header = next(i for i, line in enumerate(lines) if line.startswith("Datum"))
    df = pd.read_csv(io.StringIO("\n".join(lines[header + 1:])), sep=";", header=None,
                     usecols=[0, 1], names=["date", "flow"], parse_dates=["date"])
    return df.set_index("date").flow.asfreq("D")


def load_runoff(max_age_hours: float = 12) -> pd.Series:
    """Specifik avrinning (m³/s per km²) per dygn vid den oreglerade stationen Idre 3."""
    daily = _smhi_daily_flow(RUNOFF_STATION, max_age_hours).interpolate(limit=5)
    return (daily / RUNOFF_STATION_KM2).rename("runoff")


def load_regulated_flow(max_age_hours: float = 12) -> pd.Series:
    """Uppmätt flöde (m³/s) per dygn vid Spjutmo kraftverk, alltså det Fortum faktiskt har tappat
    genom kaskaden plus den lokala tillrinningen."""
    return _smhi_daily_flow(REGULATED_STATION, max_age_hours).rename("spjutmo")


def daily_inflows(cascade: Cascade, runoff: pd.Series | None = None,
                  until: pd.Timestamp | None = None) -> pd.DataFrame:
    """Lokal tillrinning (m³/s) per kraftverk och dygn för hela SMHI-serien. Dygn efter
    senaste observationen (fram till until) förlängs med sista värdet; det är prognosens
    sak att hantera framtiden, inte datalagrets."""
    runoff = load_runoff() if runoff is None else runoff
    days = pd.date_range(runoff.index[0], max(runoff.index[-1], until or runoff.index[-1]))
    q = runoff.reindex(days).ffill().bfill().to_numpy()
    out = {}
    for plant in cascade.plants:
        # Linjärt magasin: utflödet närmar sig inflödet med tidskonstanten response_days.
        routed = np.empty_like(q)
        routed[0] = q[0]
        alpha = 1 - np.exp(-1 / plant.response_days)
        for i in range(1, len(q)):
            routed[i] = routed[i - 1] + alpha * (q[i] - routed[i - 1])
        out[plant.name] = routed * plant.catchment_km2
    return pd.DataFrame(out, index=days)


def local_inflows(cascade: Cascade, start: pd.Timestamp, end: pd.Timestamp,
                  runoff: pd.Series | None = None, freq: str = "15min") -> pd.DataFrame:
    """Lokal tillrinning (m³/s) per kraftverk från start till end (end exklusive), per kvart eller
    timme. Dygnsmedlen läggs mitt på dygnet och interpoleras linjärt."""
    daily = daily_inflows(cascade, runoff, until=end.normalize() + pd.Timedelta(days=1))
    centers = (daily.index + pd.Timedelta(hours=12)).asi8
    times = pd.date_range(start, end, freq=freq, inclusive="left")
    return pd.DataFrame({name: np.interp(times.asi8, centers, daily[name].to_numpy()) for name in daily},
                        index=times)


def last_runoff_date(runoff: pd.Series | None = None) -> pd.Timestamp:
    runoff = load_runoff() if runoff is None else runoff
    return runoff.dropna().index[-1]
