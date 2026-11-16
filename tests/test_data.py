import numpy as np
import pandas as pd

import data
from data import parse_unit_generation

RESPONSE = b"""<?xml version="1.0" encoding="UTF-8"?>
<GL_MarketDocument xmlns="urn:iec62325.351:tc57wg16:451-6:generationloaddocument:3:0">
  <TimeSeries>
    <MktPSRType>
      <psrType>B12</psrType>
      <PowerSystemResources><mRID codingScheme="A01">46W000000000001X</mRID><name>TR\xc3\x84NGSLET G1</name></PowerSystemResources>
    </MktPSRType>
    <Period>
      <timeInterval><start>2026-01-04T23:00Z</start><end>2026-01-05T02:00Z</end></timeInterval>
      <resolution>PT60M</resolution>
      <Point><position>1</position><quantity>80</quantity></Point>
      <Point><position>3</position><quantity>95</quantity></Point>
    </Period>
  </TimeSeries>
  <TimeSeries>
    <MktPSRType>
      <psrType>B14</psrType>
      <PowerSystemResources><mRID codingScheme="A01">46W000000000002X</mRID><name>Annat verk</name></PowerSystemResources>
    </MktPSRType>
    <Period>
      <timeInterval><start>2026-01-04T23:00Z</start><end>2026-01-04T23:30Z</end></timeInterval>
      <resolution>PT15M</resolution>
      <Point><position>1</position><quantity>500</quantity></Point>
      <Point><position>2</position><quantity>510</quantity></Point>
    </Period>
  </TimeSeries>
</GL_MarketDocument>"""


def test_unit_generation_is_parsed_per_unit_in_cet():
    frame = parse_unit_generation(RESPONSE)
    unit = frame["TRÄNGSLET G1"].dropna()
    assert list(unit.index.hour) == [0, 1, 2] and unit.index[0] == pd.Timestamp("2026-01-05")
    assert list(unit) == [80, 80, 95]                    # utelämnad punkt = samma som närmast före
    assert frame["Annat verk"].dropna().tolist() == [500, 510]


def test_unit_generation_is_matched_by_name_and_spread_on_quarters(tmp_path, monkeypatch):
    monkeypatch.setattr(data, "CACHE", tmp_path)
    (tmp_path / "entsoe").mkdir()
    parse_unit_generation(RESPONSE).to_csv(tmp_path / "entsoe" / "SE_2026-01-05.csv")
    units = data.load_unit_generation(pd.Timestamp("2026-01-05"), pd.Timestamp("2026-01-05 03:00"), "Trängslet")
    assert list(units.columns) == ["TRÄNGSLET G1"]
    assert len(units) == 12 and np.allclose(units.iloc[:8, 0], 80) and np.allclose(units.iloc[8:, 0], 95)


def test_unit_generation_is_requested_for_the_swedish_control_area(tmp_path, monkeypatch):
    """ENTSO-E publicerar produktion per aggregat per kontrollområde; frågar man efter
    elområdet SE3 blir svaret tomt."""
    monkeypatch.setattr(data, "CACHE", tmp_path)
    monkeypatch.setattr(data, "entsoe_token", lambda: "nyckel")
    monkeypatch.setattr(data.time, "sleep", lambda s: None)
    urls = []
    monkeypatch.setattr(data, "_download", lambda url: urls.append(url) or RESPONSE)
    units = data.load_unit_generation(pd.Timestamp("2026-01-05"), pd.Timestamp("2026-01-06"), "Trängslet", "SE3")
    assert len(urls) == 1 and "in_Domain=10YSE-1--------K" in urls[0] and "documentType=A73" in urls[0]
    assert units["TRÄNGSLET G1"].notna().any()
