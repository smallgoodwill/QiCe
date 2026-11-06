"""期货品种清单：一张表解决"这个品种在哪、多少钱一跳、一手多大"。

为什么单独一个模块
------------------
在此之前，合约乘数散落在各处的默认值里（simulate 用 1，交易页让用户手输），
于是算出来的盈亏和真实盘口差着一个数量级。这里把品种元数据集中定义：

    multiplier  合约乘数（一手合约对应多少标的），盈亏 = 价差 × 乘数 × 手数
    tick        最小变动价位，价格必须落在它的整数倍上
    marginPct   保证金率（参考值，各家期货公司会加收，仅用于估算占用资金）
    mainMonths  主力合约月份习惯（螺纹是 1/5/10，股指是当月+下月）
    digits      合约代码月份位数：中金所/上期所/大商所/能源中心 4 位（rb2601），
                郑商所 3 位（SR601），写错一位就订阅不到行情

数据口径：乘数、最小变动取交易所合约文本；保证金率是**常见挂牌水平**，
不保证等于你账户上的实际值——页面上会标注"参考值"。

不参与交易的品种不放进来：本平台不发真实委托，这份表只用于行情订阅、
盈亏计算与图表选择，所以不需要交易权限、也不用管保证金是否够。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

# 交易所代码 -> 中文名。CTP 里 Exchange 字段用的就是左边这个枚举值。
EXCHANGES: dict[str, str] = {
    "CFFEX": "中金所",
    "SHFE": "上期所",
    "DCE": "大商所",
    "CZCE": "郑商所",
    "INE": "能源中心",
    "GFEX": "广期所",
}

# 板块用于前端分组展示，别按交易所排——用户找品种想的是"黑色系""贵金属"
SECTORS = ("金融", "黑色", "有色", "贵金属", "能化", "农产品", "航运", "新能源")


@dataclass(frozen=True)
class Instrument:
    code: str                 # 品种代码，小写（郑商所行情里是大写，这里统一小写存，输出时转）
    name: str
    exchange: str
    sector: str
    multiplier: float
    tick: float
    margin_pct: float         # 参考保证金率，百分比
    main_months: tuple[int, ...]
    digits: int = 4           # 合约月份位数

    @property
    def exchange_name(self) -> str:
        return EXCHANGES.get(self.exchange, self.exchange)

    @property
    def symbol_prefix(self) -> str:
        """交易所里实际使用的品种字母：郑商所全大写，其余小写。"""
        return self.code.upper() if self.exchange == "CZCE" else self.code

    @property
    def margin_per_lot(self) -> float:
        """按当前价估算的一手保证金，需要价格，这里只给比率说明用。"""
        return self.margin_pct

    def contract(self, year: int, month: int) -> str:
        """拼合约代码：rb2601 / SR601。"""
        if self.digits == 3:
            return f"{self.symbol_prefix}{year % 10}{month:02d}"
        return f"{self.symbol_prefix}{year % 100:02d}{month:02d}"

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "name": self.name,
            "exchange": self.exchange,
            "exchangeName": self.exchange_name,
            "sector": self.sector,
            "multiplier": self.multiplier,
            "tick": self.tick,
            "marginPct": self.margin_pct,
            "mainMonths": list(self.main_months),
            "digits": self.digits,
            "label": f"{self.name} · {self.code.upper()}",
        }


# fmt: off
# 顺序按板块排，前端直接照这个顺序渲染，省得再排一次。
_TABLE: tuple[Instrument, ...] = (
    # ---------------- 金融：中金所 ----------------
    Instrument("if", "沪深300股指", "CFFEX", "金融", 300, 0.2, 12, (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12)),
    Instrument("ih", "上证50股指", "CFFEX", "金融", 300, 0.2, 12, (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12)),
    Instrument("ic", "中证500股指", "CFFEX", "金融", 200, 0.2, 14, (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12)),
    Instrument("im", "中证1000股指", "CFFEX", "金融", 200, 0.2, 15, (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12)),
    Instrument("t", "10年期国债", "CFFEX", "金融", 10000, 0.005, 2, (3, 6, 9, 12)),
    Instrument("tf", "5年期国债", "CFFEX", "金融", 10000, 0.005, 1.2, (3, 6, 9, 12)),
    Instrument("ts", "2年期国债", "CFFEX", "金融", 20000, 0.005, 0.5, (3, 6, 9, 12)),
    Instrument("tl", "30年期国债", "CFFEX", "金融", 10000, 0.01, 3.5, (3, 6, 9, 12)),

    # ---------------- 黑色：螺纹、铁矿、双焦 ----------------
    Instrument("rb", "螺纹钢", "SHFE", "黑色", 10, 1, 10, (1, 5, 10)),
    Instrument("hc", "热卷", "SHFE", "黑色", 10, 1, 10, (1, 5, 10)),
    Instrument("ss", "不锈钢", "SHFE", "黑色", 5, 5, 12, (1, 5, 9)),
    Instrument("i", "铁矿石", "DCE", "黑色", 100, 0.5, 12, (1, 5, 9)),
    Instrument("j", "焦炭", "DCE", "黑色", 100, 0.5, 12, (1, 5, 9)),
    Instrument("jm", "焦煤", "DCE", "黑色", 60, 0.5, 12, (1, 5, 9)),
    Instrument("sf", "硅铁", "CZCE", "黑色", 5, 2, 12, (1, 5, 9), 3),
    Instrument("sm", "锰硅", "CZCE", "黑色", 5, 2, 12, (1, 5, 9), 3),

    # ---------------- 有色 ----------------
    Instrument("cu", "铜", "SHFE", "有色", 5, 10, 10, (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12)),
    Instrument("al", "铝", "SHFE", "有色", 5, 5, 10, (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12)),
    Instrument("zn", "锌", "SHFE", "有色", 5, 5, 10, (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12)),
    Instrument("pb", "铅", "SHFE", "有色", 5, 5, 10, (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12)),
    Instrument("ni", "镍", "SHFE", "有色", 1, 10, 12, (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12)),
    Instrument("sn", "锡", "SHFE", "有色", 1, 10, 12, (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12)),
    Instrument("ao", "氧化铝", "SHFE", "有色", 20, 1, 10, (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12)),
    Instrument("bc", "国际铜", "INE", "有色", 5, 10, 10, (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12)),

    # ---------------- 贵金属 ----------------
    Instrument("au", "黄金", "SHFE", "贵金属", 1000, 0.02, 9, (2, 4, 6, 8, 10, 12)),
    Instrument("ag", "白银", "SHFE", "贵金属", 15, 1, 10, (2, 4, 6, 8, 10, 12)),

    # ---------------- 能化 ----------------
    Instrument("sc", "原油", "INE", "能化", 1000, 0.1, 12, (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12)),
    Instrument("lu", "低硫燃料油", "INE", "能化", 10, 1, 12, (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12)),
    Instrument("fu", "燃料油", "SHFE", "能化", 10, 1, 12, (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12)),
    Instrument("bu", "沥青", "SHFE", "能化", 10, 1, 12, (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12)),
    Instrument("ru", "天然橡胶", "SHFE", "能化", 10, 5, 10, (1, 5, 9)),
    Instrument("br", "丁二烯橡胶", "SHFE", "能化", 5, 5, 12, (1, 5, 9)),
    Instrument("sp", "纸浆", "SHFE", "能化", 10, 2, 10, (1, 5, 9)),
    Instrument("l", "塑料", "DCE", "能化", 5, 1, 9, (1, 5, 9)),
    Instrument("v", "PVC", "DCE", "能化", 5, 1, 9, (1, 5, 9)),
    Instrument("pp", "聚丙烯", "DCE", "能化", 5, 1, 9, (1, 5, 9)),
    Instrument("eg", "乙二醇", "DCE", "能化", 10, 1, 10, (1, 5, 9)),
    Instrument("eb", "苯乙烯", "DCE", "能化", 5, 1, 10, (1, 5, 9)),
    Instrument("pg", "液化石油气", "DCE", "能化", 20, 1, 12, (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12)),
    Instrument("ta", "PTA", "CZCE", "能化", 5, 2, 9, (1, 5, 9), 3),
    Instrument("ma", "甲醇", "CZCE", "能化", 10, 1, 10, (1, 5, 9), 3),
    Instrument("fg", "玻璃", "CZCE", "能化", 20, 1, 12, (1, 5, 9), 3),
    Instrument("sa", "纯碱", "CZCE", "能化", 20, 1, 12, (1, 5, 9), 3),
    Instrument("ur", "尿素", "CZCE", "能化", 20, 1, 10, (1, 5, 9), 3),
    Instrument("pf", "短纤", "CZCE", "能化", 5, 2, 9, (1, 5, 9), 3),
    Instrument("px", "对二甲苯", "CZCE", "能化", 5, 2, 12, (1, 5, 9), 3),
    Instrument("sh", "烧碱", "CZCE", "能化", 30, 1, 12, (1, 5, 9), 3),

    # ---------------- 农产品 ----------------
    Instrument("m", "豆粕", "DCE", "农产品", 10, 1, 9, (1, 5, 9)),
    Instrument("y", "豆油", "DCE", "农产品", 10, 2, 9, (1, 5, 9)),
    Instrument("a", "豆一", "DCE", "农产品", 10, 1, 9, (1, 5, 9)),
    Instrument("b", "豆二", "DCE", "农产品", 10, 1, 9, (1, 5, 9)),
    Instrument("p", "棕榈油", "DCE", "农产品", 10, 2, 10, (1, 5, 9)),
    Instrument("c", "玉米", "DCE", "农产品", 10, 1, 8, (1, 5, 9)),
    Instrument("cs", "玉米淀粉", "DCE", "农产品", 10, 1, 8, (1, 5, 9)),
    Instrument("jd", "鸡蛋", "DCE", "农产品", 10, 1, 9, (1, 5, 9)),
    Instrument("lh", "生猪", "DCE", "农产品", 16, 5, 12, (1, 3, 5, 7, 9, 11)),
    Instrument("sr", "白糖", "CZCE", "农产品", 10, 1, 9, (1, 5, 9), 3),
    Instrument("cf", "棉花", "CZCE", "农产品", 5, 5, 9, (1, 5, 9), 3),
    Instrument("oi", "菜油", "CZCE", "农产品", 10, 1, 9, (1, 5, 9), 3),
    Instrument("rm", "菜粕", "CZCE", "农产品", 10, 1, 9, (1, 5, 9), 3),
    Instrument("ap", "苹果", "CZCE", "农产品", 10, 1, 12, (1, 5, 10), 3),
    Instrument("cj", "红枣", "CZCE", "农产品", 5, 5, 12, (1, 5, 9), 3),
    Instrument("pk", "花生", "CZCE", "农产品", 5, 2, 10, (1, 5, 10), 3),

    # ---------------- 航运 ----------------
    Instrument("ec", "集运欧线", "INE", "航运", 50, 0.1, 15, (2, 4, 6, 8, 10, 12)),

    # ---------------- 新能源：广期所 ----------------
    Instrument("si", "工业硅", "GFEX", "新能源", 5, 5, 12, (1, 5, 9)),
    Instrument("lc", "碳酸锂", "GFEX", "新能源", 1, 50, 15, (1, 5, 9)),
    Instrument("ps", "多晶硅", "GFEX", "新能源", 3, 5, 12, (1, 5, 9)),
)
# fmt: on

BY_CODE: dict[str, Instrument] = {item.code: item for item in _TABLE}


def all_instruments() -> list[dict]:
    """给前端的完整清单，已按板块分好组。"""
    groups: dict[str, list[dict]] = {sector: [] for sector in SECTORS}
    for item in _TABLE:
        groups.setdefault(item.sector, []).append(item.to_dict())
    return [{"sector": sector, "items": items} for sector, items in groups.items() if items]


def active_contracts(instrument: Instrument, today: date | None = None,
                     horizon_months: int = 14) -> list[dict]:
    """按主力月份习惯，列出未来一段时间内可订阅的合约。

    不追求和交易所挂牌完全一致（那需要每日更新合约表），只保证：
    1. 生成的代码格式正确（郑商所 3 位、其余 4 位）；
    2. 不列出已经到期的月份。
    """
    today = today or date.today()
    rows: list[dict] = []
    for offset in range(horizon_months):
        year = today.year + (today.month - 1 + offset) // 12
        month = (today.month - 1 + offset) % 12 + 1
        if month not in instrument.main_months:
            continue
        # 交割月在当月及之后才算还在交易；月初就当作本月仍可交易
        last_day = (date(year + (month == 12), month % 12 + 1, 1) - timedelta(days=1))
        if last_day < today:
            continue
        code = instrument.contract(year, month)
        rows.append({
            "symbol": code,
            "year": year,
            "month": month,
            "label": f"{code} · {year}-{month:02d}",
            "isMain": False,
        })
    if rows:
        rows[0]["isMain"] = True      # 最近的未到期月份即当作主力，只作默认值
    return rows


def get(code: str) -> Instrument | None:
    return BY_CODE.get(str(code or "").strip().lower())


def guess(symbol: str) -> Instrument | None:
    """从任意字符串里认出品种。

    要处理的真实输入长这样：
        "rb2601"                    正常合约
        "IF2612"                    大写
        "SR601"                     郑商所 3 位
        "沪深300股指 · IF2503"       策略库里存的展示名（历史遗留，必须兼容）
        "螺纹钢"                     只有中文名
    """
    text = str(symbol or "").strip()
    if not text:
        return None
    lowered = text.lower()
    if lowered in BY_CODE:
        return BY_CODE[lowered]

    # 先按字母前缀匹配最长的品种代码，避免 "i" 把 "if2612" 抢走
    letters = "".join(ch for ch in text if ch.isascii() and ch.isalpha()).lower()
    for size in range(len(letters), 1, -1):
        candidate = BY_CODE.get(letters[:size])
        if candidate:
            return candidate
    if letters and letters[:1] in BY_CODE and len(letters) == 1:
        return BY_CODE[letters[:1]]

    # 中文名匹配：允许 "螺纹钢主力" 这种后缀
    for item in _TABLE:
        if item.name in text:
            return item
    return None


def meta(symbol: str) -> dict | None:
    """品种元数据 + 合约解析结果，供盈亏计算与前端展示。"""
    found = guess(symbol)
    if not found:
        return None
    digits = "".join(ch for ch in str(symbol) if ch.isdigit())
    return {**found.to_dict(), "symbol": str(symbol).strip(), "contractMonth": digits or None}


def default_multiplier(symbol: str) -> float:
    found = guess(symbol)
    return found.multiplier if found else 1.0


def default_tick(symbol: str) -> float:
    found = guess(symbol)
    return found.tick if found else 0.01
