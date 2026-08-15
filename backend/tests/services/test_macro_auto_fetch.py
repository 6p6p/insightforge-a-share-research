"""Unit tests for macro auto-fetch mapping (V1.1 closure)."""

from app.services.macro_auto_fetch_service import (
    resolve_macro_country,
    resolve_macro_indicator,
)


def test_resolve_macro_indicator_exact() -> None:
    assert resolve_macro_indicator("GDP增长率") == "NY.GDP.MKTP.KD.ZG"
    assert resolve_macro_indicator("gdp增长率") == "NY.GDP.MKTP.KD.ZG"
    assert resolve_macro_indicator("CPI") == "FP.CPI.TOTL.ZG"
    assert resolve_macro_indicator("失业率") == "SL.UEM.TOTL.ZS"
    assert resolve_macro_indicator("总人口") == "SP.POP.TOTL"


def test_resolve_macro_indicator_suffix() -> None:
    assert resolve_macro_indicator("GDP增长率（%）") == "NY.GDP.MKTP.KD.ZG"
    assert resolve_macro_indicator("GDP总量（现价美元）") == "NY.GDP.MKTP.CD"


def test_resolve_macro_indicator_unknown() -> None:
    assert resolve_macro_indicator("锂电池出货量") is None
    assert resolve_macro_indicator("") is None
    assert resolve_macro_indicator(None) is None


def test_resolve_macro_country() -> None:
    assert resolve_macro_country("中国") == "CHN"
    assert resolve_macro_country("CHN") == "CHN"
    assert resolve_macro_country("china") == "CHN"
    assert resolve_macro_country("美国") == "USA"
    assert resolve_macro_country("USA") == "USA"
    assert resolve_macro_country("火星") == "CHN"  # 未命中 → CHN 默认
    assert resolve_macro_country(None) == "CHN"
