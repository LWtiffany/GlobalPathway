"""Curated HSBC local deposit-account catalog for study destinations.

Agent1 ACCOUNT_THRESHOLD is empty; this module is the interim verified product
surface for BankAccountWorkflow. Only HSBC products with official URLs are listed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from .models import Citation, Material
from .retrieve import display_url

NOW = datetime(2026, 10, 27, tzinfo=timezone.utc)

IDENTITY_LABEL = {"student": "学生本人", "parent": "家长", "other": "其他申请人"}
REGION_LABEL = {"UK": "英国", "US": "美国", "CN-HK": "中国香港", "CN": "中国内地"}


@dataclass(frozen=True)
class HsbcAccountProduct:
    product_id: str
    region: str
    name_zh: str
    name_en: str
    audiences: tuple[str, ...]
    summary: str
    eligibility: tuple[str, ...]
    process_steps: tuple[str, ...]
    materials: tuple[str, ...]
    source_name: str
    source_url: str
    notes: tuple[str, ...] = ()


# Official HSBC pages only — do not invent non-HSBC banks.
HSBC_PRODUCTS: tuple[HsbcAccountProduct, ...] = (
    HsbcAccountProduct(
        product_id="uk_international_student",
        region="UK",
        name_zh="汇丰英国国际学生往来账户",
        name_en="HSBC UK International Student Bank Account",
        audiences=("student",),
        summary="面向在英国高校就读的国际学生的往来账户，无月费（以官网为准）。",
        eligibility=(
            "年满 18 岁",
            "能提供有效身份证明与地址证明",
            "持有英国大学/学院合格课程录取证明",
        ),
        process_steps=(
            "在汇丰英国官网比较 International Student 账户，确认是否符合国际学生路径。",
            "准备护照、地址证明与录取证明；可按官网指引在抵达前后申请。",
            "按提示完成身份核验；若官网提示改走其他账户（如 HSBC Bank Account），以当时页面为准。",
            "开户后在 App/网银激活借记卡与收款信息，再安排学费或生活费入账。",
        ),
        materials=("护照或其他有效身份证件", "地址证明", "英国高校录取/在读证明"),
        source_name="HSBC UK International Student Bank Account",
        source_url="https://www.hsbc.co.uk/current-accounts/compare/international-student-bank-account/",
        notes=("官网说明：新到英国或居英未满约 3 年时，可能更适合其他汇丰往来账户路径。",),
    ),
    HsbcAccountProduct(
        product_id="uk_student",
        region="UK",
        name_zh="汇丰英国学生账户",
        name_en="HSBC UK Student Bank Account",
        audiences=("student",),
        summary="面向已在英国居住且获合格课程录取的学生往来账户，无月费（以官网为准）。",
        eligibility=(
            "年满 18 岁",
            "在英国（或 Channel Islands / Isle of Man）居住满约 3 年",
            "持有合格课程录取证明（如 UCAS 状态码或院校录取信）",
        ),
        process_steps=(
            "在汇丰英国 Student Bank Account 页面确认资格与所需材料。",
            "在线申请时准备近 3 年地址、收入/课程信息与录取证明。",
            "按需补充身份、地址与移民身份核验材料。",
            "获批后完成网银/App 注册并核对账户可用于日常支付与收款。",
        ),
        materials=("身份证明", "地址证明", "录取证明（UCAS 码或院校录取信）"),
        source_name="HSBC UK Student Bank Account",
        source_url="https://www.hsbc.co.uk/current-accounts/products/student/",
        notes=("若不符合居英年限，官网建议改看 HSBC Bank Account / 国际学生相关路径。",),
    ),
    HsbcAccountProduct(
        product_id="uk_bank_account_new_to_uk",
        region="UK",
        name_zh="汇丰英国银行账户（新到英国）",
        name_en="HSBC UK Bank Account (apply from outside the UK)",
        audiences=("student", "parent", "other"),
        summary="可在境外申请的汇丰英国往来账户路径，适合尚未满足学生账户居留条件的申请人。",
        eligibility=(
            "年满 18 岁",
            "能提供身份证明与地址证明",
            "接受汇丰必要的身份与信用核验（以申请时规则为准）",
        ),
        process_steps=(
            "打开汇丰「Apply for a UK account」页面，选择适合的账户并开始申请。",
            "优先用 HSBC App 提交；也可抵达后到英国网点办理。",
            "准备护照/驾照等身份证明，以及水电账单或银行账单等地址证明。",
            "按申请进度补充税务与就业/就读信息，获批后激活账户。",
        ),
        materials=("护照或驾照", "地址证明", "就业/收入或就读相关信息（按申请表要求）"),
        source_name="HSBC UK Apply for a UK account",
        source_url="https://www.hsbc.co.uk/international/apply-for-a-uk-account/",
    ),
    HsbcAccountProduct(
        product_id="us_premier_checking",
        region="US",
        name_zh="汇丰美国卓越理财支票账户",
        name_en="HSBC USA Premier Checking",
        audiences=("student", "parent", "other"),
        summary="汇丰美国面向留学与跨境客户的主流支票账户；国际学生页面说明可在抵达前安排银行服务。",
        eligibility=(
            "年满 18 岁",
            "位于汇丰允许在线申请的国家/地区（名单以官网为准）",
            "学生可能需提供在读/录取证明；可能涉及 SSN 或税务声明",
        ),
        process_steps=(
            "阅读汇丰美国「Studying in the U.S.」与 Premier Checking 页面，确认是否走国际学生开户路径。",
            "按官网在线申请或预约协助；准备护照、地址证明，学生另备录取/在读证明。",
            "按提示完成税务信息（SSN 或有效税务声明）与身份核验。",
            "获批后注册网银/App，核对入金与跨境转账能力后再安排学费或生活费。",
        ),
        materials=("护照", "地址证明", "学生录取/在读证明（如适用）", "SSN 或税务声明材料"),
        source_name="HSBC USA Studying in the U.S.",
        source_url="https://www.us.hsbc.com/international-banking/studying-in-the-us/",
        notes=(
            "免月费通常需满足关系余额/入账等条件，否则可能产生月费；以 Premier 产品页当时规则为准。",
            "产品详情页：https://www.us.hsbc.com/checking-accounts/products/premier/",
        ),
    ),
    HsbcAccountProduct(
        product_id="hk_app_account",
        region="CN-HK",
        name_zh="汇丰香港个人银行账户（App 开户）",
        name_en="HSBC Hong Kong account opening via HSBC HK App",
        audiences=("student", "parent", "other"),
        summary="可通过 HSBC HK App 开立香港汇丰账户；留学生亦可参考到港网点或 App 路径。",
        eligibility=(
            "年龄约 18–75 岁（以官网为准）",
            "持有合资格香港身份证、海外护照或往来港澳通行证等（路径不同）",
            "位于指定国家/地区提交申请（国际客户路径）",
        ),
        process_steps=(
            "下载 HSBC HK App，选择新客户开户并按提示拍摄证件与自拍核验。",
            "填写个人资料并设置网银；国际客户按护照/通行证路径上传材料。",
            "亦可参考「How to open a Hong Kong account」预约网点或热线办理。",
            "获批后在港激活/使用账户，再安排学费或生活费收款。",
        ),
        materials=("香港身份证或护照/通行证", "自拍核验", "地址证明（按申请要求）"),
        source_name="HSBC Hong Kong account opening",
        source_url="https://www.hsbc.com.hk/ways-to-bank/mobile-apps/banking/account-opening/",
        notes=("留学场景可同时参考：https://www.hsbc.com.hk/international/studying-in-hong-kong/",),
    ),
)


@dataclass
class HsbcAccountBundle:
    region: str
    identity: str
    products: list[HsbcAccountProduct]
    citations: list[Citation] = field(default_factory=list)
    materials: list[Material] = field(default_factory=list)

    @property
    def region_zh(self) -> str:
        return REGION_LABEL.get(self.region, self.region)

    @property
    def identity_zh(self) -> str:
        return IDENTITY_LABEL.get(self.identity, self.identity)


def products_for(*, region: str, identity: str | None = None) -> list[HsbcAccountProduct]:
    rows = [p for p in HSBC_PRODUCTS if p.region == region]
    if identity:
        preferred = [p for p in rows if identity in p.audiences]
        if preferred:
            return preferred
    return list(rows)


def build_account_bundle(*, region: str, identity: str) -> HsbcAccountBundle | None:
    products = products_for(region=region, identity=identity)
    if not products and region == "CN":
        return None
    if not products:
        products = products_for(region=region)
    if not products:
        return None

    citations: list[Citation] = []
    materials: list[Material] = []
    seen_urls: set[str] = set()
    for product in products:
        url = display_url(product.source_url) or product.source_url
        if url in seen_urls:
            cid = next(c.citation_id for c in citations if c.source_url == url)
        else:
            cid = f"C{len(citations) + 1}"
            citations.append(Citation(
                citation_id=cid,
                record_id=f"hsbc-account:{product.product_id}",
                revision_id="hsbc-account-catalog-1",
                evidence_id=product.product_id,
                source_name=product.source_name,
                source_url=url,
                quote=product.summary,
                verified_at=NOW,
                synthetic=False,
            ))
            seen_urls.add(url)
        for name in product.materials:
            key = f"{product.product_id}:{name}"
            if any(m.requirement_key == key for m in materials):
                continue
            materials.append(Material(
                requirement_key=key,
                document_code="account_document",
                name_zh=name,
                name_en="",
                applicability="required",
                condition_text=f"办理「{product.name_zh}」时常用",
                citation_id=cid,
            ))
    return HsbcAccountBundle(
        region=region, identity=identity, products=products,
        citations=citations, materials=materials,
    )


def verified_payload(bundle: HsbcAccountBundle) -> dict:
    return {
        "bank": "HSBC",
        "region": bundle.region,
        "region_zh": bundle.region_zh,
        "applicant_identity": bundle.identity,
        "applicant_identity_zh": bundle.identity_zh,
        "rule": "只推荐汇丰在当地提供的存款/往来账户；不推荐其他银行。",
        "products": [
            {
                "product_id": p.product_id,
                "name_zh": p.name_zh,
                "name_en": p.name_en,
                "summary": p.summary,
                "eligibility": list(p.eligibility),
                "process_steps": list(p.process_steps),
                "materials": list(p.materials),
                "source_name": p.source_name,
                "source_url": p.source_url,
                "notes": list(p.notes),
            }
            for p in bundle.products
        ],
    }
