"""Synchronize the reviewed HSBC UK/HK/US personal deposit-account catalog to RDS.

Scope: active consumer current/checking, integrated, savings and fixed-deposit products
shown in the three official HSBC product catalogs on 2026-10-28. Business accounts,
credit cards, loans, investments and legacy/retained-only products are excluded.

The command is dry-run by default. Use --apply to execute one transaction. It never
prints credentials and performs idempotent upserts under an advisory transaction lock.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent1 import storage  # noqa: E402

VERIFIED_AT = datetime(2026, 10, 29, tzinfo=timezone.utc)

UK_CURRENT = "https://www.hsbc.co.uk/current-accounts/compare/"
UK_SAVINGS = "https://www.hsbc.co.uk/savings/compare/"
HK_ACCOUNTS = "https://www.hsbc.com.hk/accounts/"
HK_CURRENT = "https://www.hsbc.com.hk/accounts/products/current/"
HK_SAVINGS = "https://servicing.hsbc.com.hk/accounts/products/savings/hk-dollar/"
HK_TIME_DEPOSIT = "https://www.hsbc.com.hk/accounts/products/time-deposits/"
HK_BASIC_INDEPENDENCE = "https://www.hsbc.com.hk/community-banking/age-friendly/basic-banking-account/"
HK_OPENING_NOTES = "https://www.hsbc.com.hk/content/dam/hsbc/hk/docs/accounts/applyaccount-note-en.pdf"
US_SITE_MAP = "https://www.us.hsbc.com/site-map/"


def product(account_id, code, name, market, account_type, segment, currency, summary,
            url, *, student=0):
    return (account_id, code, name, market, account_type, segment, currency, summary,
            student, "ACTIVE", url, "verified", None, None, VERIFIED_AT)


PRODUCTS = [
    # UK current accounts: official current-account comparison catalog.
    product("acct_uk_bank", "uk_bank", "HSBC Bank Account", "UK", "current", "general", "GBP",
            "英国日常往来账户，无月费。", UK_CURRENT),
    product("acct_uk_premier", "uk_premier", "HSBC Premier Bank Account", "UK", "current", "premier", "GBP",
            "面向符合财务资格客户的英国卓越理财往来账户，无月费。", UK_CURRENT),
    product("acct_uk_private_banking", "uk_private_banking", "HSBC Private Banking Account", "UK", "current", "premier", "GBP",
            "面向符合私人银行资格客户的英国往来账户。", UK_CURRENT),
    product("acct_uk_student", "uk_student", "HSBC Student Bank Account", "UK", "current", "student", "GBP",
            "为符合条件的英国高校学生提供的往来账户。", "https://www.hsbc.co.uk/current-accounts/products/student/", student=1),
    product("acct_uk_graduate", "uk_graduate", "HSBC Graduate Bank Account", "UK", "current", "student", "GBP",
            "面向毕业两年内申请人的英国毕业生账户。", "https://www.hsbc.co.uk/current-accounts/products/graduate/", student=1),
    product("acct_uk_basic", "uk_basic", "HSBC Basic Bank Account", "UK", "current", "general", "GBP",
            "面向不符合其他往来账户条件或没有标准身份证明的基础账户。", UK_CURRENT),
    product("acct_uk_children_current", "uk_children_current", "HSBC Children's Bank Account", "UK", "current", "general", "GBP",
            "面向11至17岁青少年的往来账户，附带借记卡和关联储蓄账户。", UK_CURRENT, student=1),
    product("acct_uk_international_student", "uk_international_student", "HSBC Bank Account for international students", "UK", "current", "international_student", "GBP",
            "面向赴英国际学生的开户路径和日常往来账户。", "https://www.hsbc.co.uk/international/education/student-bank-account/", student=1),
    # UK savings catalog.
    product("acct_uk_online_bonus_savings", "uk_online_bonus_savings", "Online Bonus Savings Account", "UK", "savings", "general", "GBP",
            "未取款月份可获得较高利率的网上储蓄账户，仅限现有汇丰客户。", UK_SAVINGS),
    product("acct_uk_fixed_rate_savings", "uk_fixed_rate_savings", "Fixed Rate Savings Account", "UK", "savings", "general", "GBP",
            "一至两年定期储蓄，单笔存入2,000至1,000,000英镑。", "https://www.hsbc.co.uk/savings/products/fixed-rate/"),
    product("acct_uk_fixed_rate_cash_isa", "uk_fixed_rate_cash_isa", "Fixed Rate Cash ISA", "UK", "savings", "general", "GBP",
            "固定期限免税现金ISA，仅限符合条件的英国往来账户客户。", UK_SAVINGS),
    product("acct_uk_loyalty_cash_isa", "uk_loyalty_cash_isa", "Loyalty Cash ISA", "UK", "savings", "general", "GBP",
            "可随时存取的免税现金ISA，仅限符合条件的英国往来账户客户。", UK_SAVINGS),
    product("acct_uk_flexible_savings", "uk_flexible_savings", "Flexible Savings Account", "UK", "savings", "general", "GBP",
            "可灵活存取的英国储蓄账户。", UK_SAVINGS),
    product("acct_uk_regular_savings", "uk_regular_savings", "Regular Savings Account", "UK", "savings", "general", "GBP",
            "每月存入25至250英镑的12个月定期储蓄账户。", UK_SAVINGS),
    product("acct_uk_premier_savings", "uk_premier_savings", "HSBC Premier Savings", "UK", "savings", "premier", "GBP",
            "仅向英国汇丰卓越理财客户提供的灵活储蓄账户。", UK_SAVINGS),
    product("acct_uk_children_savings", "uk_children_savings", "Children's Savings Account", "UK", "savings", "general", "GBP",
            "面向7至17岁儿童和青少年的储蓄账户，最低可从10英镑开始。", UK_SAVINGS, student=1),
    # US official personal banking catalog.
    product("acct_us_premier", "us_premier_checking", "HSBC Premier Checking", "US", "checking", "premier", "USD",
            "美国汇丰卓越理财支票账户；维持任一关系资格可免月费。", "https://www.us.hsbc.com/checking-accounts/products/premier/"),
    product("acct_us_global_money", "us_global_money", "HSBC Global Money Account", "US", "checking", "premier", "USD",
            "仅限手机银行使用的多币种预付账户，需要合资格的美国汇丰账户作为资金来源。", "https://www.us.hsbc.com/checking-accounts/products/global-money/"),
    product("acct_us_premier_savings", "us_premier_savings", "HSBC Premier Savings", "US", "savings", "premier", "USD",
            "面向美国汇丰卓越理财客户的分层利率储蓄账户。", "https://www.us.hsbc.com/savings-accounts/products/premier/"),
    product("acct_us_premier_relationship_savings", "us_premier_relationship_savings", "HSBC Premier Relationship Savings", "US", "savings", "premier", "USD",
            "与合资格Premier Checking关联、满足条件可获得关系利率的储蓄账户。", "https://www.us.hsbc.com/savings-accounts/products/relationship-savings/"),
    product("acct_us_new_money_cd", "us_new_money_cd", "HSBC New Money Certificate of Deposit", "US", "savings", "premier", "USD",
            "使用新资金开立的美国定期存款，提供6、9、12或24个月期限。", "https://www.us.hsbc.com/savings-accounts/products/new-money-certificate-of-deposit/"),
    product("acct_us_online_cd", "us_online_cd", "HSBC Online Certificate of Deposit", "US", "savings", "premier", "USD",
            "仅限网上开立的美国定期存款，提供6、12或24个月期限。", "https://www.us.hsbc.com/savings-accounts/products/certificate-of-deposit/"),
    # HK official accounts catalog.
    product("acct_hk_one", "hk_one", "HSBC One", "HK", "integrated", "general", "HKD",
            "包含港元往来、储蓄、多币种和定期存款服务的香港综合账户。", "https://www.hsbc.com.hk/accounts/products/one/"),
    product("acct_hk_premier", "hk_premier", "HSBC Premier", "HK", "integrated", "premier", "HKD",
            "要求维持100万港元总关系结余的香港卓越理财综合账户。", "https://www.hsbc.com.hk/premier/"),
    product("acct_hk_premier_elite", "hk_premier_elite", "HSBC Premier Elite", "HK", "integrated", "premier", "HKD",
            "面向维持780万港元总关系结余的香港卓越理财尊尚客户。", "https://www.hsbc.com.hk/premier-elite/"),
    product("acct_hk_teens", "hk_teens", "HSBC Teens", "HK", "integrated", "student", "HKD",
            "面向11至17岁香港青少年的手机综合账户。", "https://www.hsbc.com.hk/accounts/products/teens/", student=1),
    product("acct_hk_children_savings", "hk_children_savings", "HSBC Children Savings Account", "HK", "savings", "general", "HKD",
            "面向未满18岁儿童的港元储蓄账户。", "https://www.hsbc.com.hk/accounts/products/savings/children/", student=1),
    product("acct_hk_hkd_savings", "hk_hkd_savings", "Hong Kong Dollar Savings Account", "HK", "savings", "general", "HKD",
            "可赚取利息并随时存取的港元储蓄账户。", HK_SAVINGS),
    product("acct_hk_hkd_current", "hk_hkd_current", "Hong Kong Dollar Current Account", "HK", "current", "general", "HKD",
            "用于日常付款和签发港元支票的港元往来账户。", HK_CURRENT),
    product("acct_hk_time_deposit", "hk_time_deposit", "HSBC Time Deposit", "HK", "savings", "general", "HKD",
            "可选择港元、人民币或主要外币并按固定期限存放的定期存款。", HK_TIME_DEPOSIT),
    product("acct_hk_basic_independence", "hk_basic_independence", "Basic Banking Account with Independence", "HK", "current", "general", "HKD",
            "为心智能力受损客户设计、由获授权人士协助管理的日常基础银行账户。", HK_BASIC_INDEPENDENCE),
]

PRODUCT_BY_ID = {row[0]: row for row in PRODUCTS}

RELATION_DDL = """
CREATE TABLE IF NOT EXISTS account_product_relation (
    relation_id TEXT PRIMARY KEY,
    source_account_id TEXT NOT NULL REFERENCES account_product(account_id) ON DELETE CASCADE,
    target_account_id TEXT NOT NULL REFERENCES account_product(account_id) ON DELETE CASCADE,
    relation_type TEXT NOT NULL CHECK (relation_type IN
        ('requires_relationship','companion_to','tier_of','alternative_to')),
    description TEXT NOT NULL,
    source_url TEXT NOT NULL,
    last_verified_at DATE NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_account_relation_source
    ON account_product_relation(source_account_id, relation_type);
CREATE INDEX IF NOT EXISTS idx_account_relation_target
    ON account_product_relation(target_account_id, relation_type);
"""

RELATIONS = [
    ("rel_us_global_money_checking", "acct_us_global_money", "acct_us_premier",
     "requires_relationship", "须使用本人名下或单签联名的合资格美国汇丰账户作为资金来源；Premier Checking 是可用关系之一。",
     PRODUCT_BY_ID["acct_us_global_money"][10], VERIFIED_AT),
    ("rel_us_global_money_savings", "acct_us_global_money", "acct_us_premier_savings",
     "requires_relationship", "Premier Savings 是 Global Money Account 可使用的合资格资金来源之一。",
     PRODUCT_BY_ID["acct_us_global_money"][10], VERIFIED_AT),
    ("rel_us_global_money_relationship_savings", "acct_us_global_money",
     "acct_us_premier_relationship_savings", "requires_relationship",
     "Premier Relationship Savings 是 Global Money Account 可使用的合资格资金来源之一。",
     PRODUCT_BY_ID["acct_us_global_money"][10], VERIFIED_AT),
    ("rel_us_premier_savings_checking", "acct_us_premier_savings", "acct_us_premier",
     "companion_to", "须持有美国汇丰 Premier 关系及 Premier Checking。",
     PRODUCT_BY_ID["acct_us_premier_savings"][10], VERIFIED_AT),
    ("rel_us_relationship_savings_checking", "acct_us_premier_relationship_savings",
     "acct_us_premier", "companion_to", "须持有美国汇丰 Premier 关系及新开或现有 Premier Checking。",
     PRODUCT_BY_ID["acct_us_premier_relationship_savings"][10], VERIFIED_AT),
    ("rel_us_new_money_cd_checking", "acct_us_new_money_cd", "acct_us_premier",
     "companion_to", "须为美国汇丰 Premier 客户并持有 Premier Checking。",
     PRODUCT_BY_ID["acct_us_new_money_cd"][10], VERIFIED_AT),
    ("rel_us_online_cd_checking", "acct_us_online_cd", "acct_us_premier",
     "companion_to", "须为美国汇丰 Premier 客户并持有 Premier Checking。",
     PRODUCT_BY_ID["acct_us_online_cd"][10], VERIFIED_AT),
    ("rel_hk_premier_elite_tier", "acct_hk_premier_elite", "acct_hk_premier",
     "tier_of", "HSBC Premier Elite 是 HSBC Premier 之上的服务层级。",
     PRODUCT_BY_ID["acct_hk_premier_elite"][10], VERIFIED_AT),
    ("rel_hk_time_deposit_one", "acct_hk_time_deposit", "acct_hk_one",
     "requires_relationship", "新客户须先开立或升级至 HSBC One 或 HSBC Premier，成功后下一工作日才可在 App 或网上银行开立定期存款。",
     PRODUCT_BY_ID["acct_hk_time_deposit"][10], VERIFIED_AT),
    ("rel_hk_time_deposit_premier", "acct_hk_time_deposit", "acct_hk_premier",
     "requires_relationship", "HSBC Premier 是新客户开立定期存款前可先建立的综合账户关系之一。",
     PRODUCT_BY_ID["acct_hk_time_deposit"][10], VERIFIED_AT),
    ("rel_hk_teens_one", "acct_hk_teens", "acct_hk_one",
     "alternative_to", "HSBC Teens 客户年满 18 岁后可保留原账户号码并按需要升级至 HSBC One。",
     PRODUCT_BY_ID["acct_hk_teens"][10], VERIFIED_AT),
]


def route(account_id, channel, name, url, *, location=None, identity=None,
          before=0, branch=0, activation=None, notes=None):
    return (f"route_{account_id[5:]}", account_id, name, channel, location, identity,
            before, branch, activation, None, notes, url, VERIFIED_AT)


ROUTES = []
for row in PRODUCTS:
    account_id, _, name, market, account_type = row[:5]
    url = row[10]
    if market == "UK":
        branch_only = account_id in {"acct_uk_graduate", "acct_uk_basic", "acct_uk_private_banking"}
        channel = "branch" if branch_only else "online"
        ROUTES.append(route(account_id, channel, f"{name}申请", url, location="英国",
                            identity="身份证明及地址证明", branch=int(branch_only),
                            notes="具体渠道和资格以产品申请页面为准。"))
    elif market == "US":
        mobile = account_id == "acct_us_global_money"
        ROUTES.append(route(account_id, "mobile_app" if mobile else "online", f"{name}申请", url,
                            location="美国或官网允许的地区", identity="政府签发身份证件",
                            notes="提交后银行会通知是否可在线完成或需要补充资料。"))
    else:
        mobile = account_id in {"acct_hk_one", "acct_hk_teens"}
        branch_only = account_id in {"acct_hk_premier_elite", "acct_hk_children_savings",
                                     "acct_hk_hkd_current", "acct_hk_basic_independence"}
        channel = "mobile_app" if mobile else "branch" if branch_only else "online"
        ROUTES.append(route(account_id, channel, f"{name}申请", url, location="香港",
                            identity="香港身份证或官网接受的其他身份证件",
                            branch=int(branch_only), notes="具体开户渠道以香港汇丰申请页面为准。"))


def rule(rule_id, account_id, rule_type, operator, description, url, *, route_id=None,
         text=None, number=None, currency=None, required=1):
    return (rule_id, account_id, route_id, rule_type, operator, text, number, currency,
            required, description, url, VERIFIED_AT)


RULES = []
ADULT_IDS = [r[0] for r in PRODUCTS if r[3] in {"UK", "US"} and r[0] not in {
    "acct_uk_children_current", "acct_uk_children_savings"}]
for account_id in ADULT_IDS:
    RULES.append(rule(f"elig_{account_id[5:]}_age", account_id, "minimum_age", "gte",
                      "申请人须年满18岁。", PRODUCT_BY_ID[account_id][10], number=18))

RULES += [
    rule("elig_uk_student_residency", "acct_uk_student", "residency_duration", "gte",
         "过去三年居住在英国、海峡群岛或马恩岛。", "https://www.hsbc.co.uk/current-accounts/products/student/", number=3),
    rule("elig_uk_student_course", "acct_uk_student", "course_status", "eq",
         "须持有符合条件课程的录取证明。", "https://www.hsbc.co.uk/current-accounts/products/student/", text="true"),
    rule("elig_uk_graduate_recency", "acct_uk_graduate", "course_status", "required",
         "须在最近两年内大学毕业。", "https://www.hsbc.co.uk/current-accounts/products/graduate/"),
    rule("elig_uk_graduate_residency", "acct_uk_graduate", "residency_duration", "gte",
         "过去三年居住在英国、海峡群岛或马恩岛。", "https://www.hsbc.co.uk/current-accounts/products/graduate/", number=3),
    rule("elig_uk_children_current_min_age", "acct_uk_children_current", "minimum_age", "gte",
         "申请人须年满11岁。", UK_CURRENT, number=11),
    rule("elig_uk_children_current_max_age", "acct_uk_children_current", "maximum_age", "lte",
         "申请人须未满18岁。", UK_CURRENT, number=17),
    rule("elig_uk_children_savings_min_age", "acct_uk_children_savings", "minimum_age", "gte",
         "适用于7至17岁。", UK_SAVINGS, number=7),
    rule("elig_uk_children_savings_max_age", "acct_uk_children_savings", "maximum_age", "lte",
         "适用于7至17岁。", UK_SAVINGS, number=17),
    rule("elig_uk_fixed_rate_min_deposit", "acct_uk_fixed_rate_savings", "minimum_deposit", "gte",
         "最低单笔存款为2,000英镑。", "https://www.hsbc.co.uk/savings/products/fixed-rate/", number=2000, currency="GBP"),
    rule("elig_uk_online_bonus_existing", "acct_uk_online_bonus_savings", "existing_customer", "eq",
         "仅限现有汇丰客户网上开立。", UK_SAVINGS, text="true"),
    rule("elig_uk_regular_existing", "acct_uk_regular_savings", "existing_customer", "eq",
         "须持有合资格的汇丰英国往来账户。", UK_SAVINGS, text="true"),
    rule("elig_us_premier_relationship_savings", "acct_us_premier_relationship_savings", "existing_customer", "eq",
         "须持有美国汇丰Premier关系及新开或现有Premier Checking。", PRODUCT_BY_ID["acct_us_premier_relationship_savings"][10], text="true"),
    rule("elig_us_premier_savings_relationship", "acct_us_premier_savings", "existing_customer", "eq",
         "须持有美国汇丰Premier关系及Premier Checking。", PRODUCT_BY_ID["acct_us_premier_savings"][10], text="true"),
    rule("elig_us_global_money_funding", "acct_us_global_money", "existing_customer", "one_of",
         "须持有本人名下或单签联名的Premier Checking、Premier Savings或Premier Relationship Savings，并使用美国汇丰手机银行。", PRODUCT_BY_ID["acct_us_global_money"][10], text="premier_checking|premier_savings|premier_relationship_savings"),
    rule("elig_us_new_money_cd_relationship", "acct_us_new_money_cd", "existing_customer", "eq",
         "须为美国汇丰Premier客户并持有Premier Checking。", PRODUCT_BY_ID["acct_us_new_money_cd"][10], text="true"),
    rule("elig_us_new_money_cd_minimum", "acct_us_new_money_cd", "minimum_deposit", "gte",
         "最低存款为1,000美元，且须使用符合定义的新资金。", PRODUCT_BY_ID["acct_us_new_money_cd"][10], number=1000, currency="USD"),
    rule("elig_us_online_cd_relationship", "acct_us_online_cd", "existing_customer", "eq",
         "须为美国汇丰Premier客户并持有Premier Checking。", PRODUCT_BY_ID["acct_us_online_cd"][10], text="true"),
    rule("elig_us_online_cd_minimum", "acct_us_online_cd", "minimum_deposit", "gte",
         "最低存款为1,000美元。", PRODUCT_BY_ID["acct_us_online_cd"][10], number=1000, currency="USD"),
    rule("elig_hk_premier_trb", "acct_hk_premier", "relationship_balance", "gte",
         "过去三个月平均总关系结余须达到1,000,000港元。", PRODUCT_BY_ID["acct_hk_premier"][10], number=1000000, currency="HKD"),
    rule("elig_hk_premier_adult", "acct_hk_premier", "minimum_age", "gte",
         "成年个人账户申请人须年满18岁。", PRODUCT_BY_ID["acct_hk_premier"][10], number=18),
    rule("elig_hk_one_adult", "acct_hk_one", "minimum_age", "gte",
         "成年HSBC One申请人须年满18岁；未成年人可查看HSBC Teens或儿童储蓄账户。", PRODUCT_BY_ID["acct_hk_one"][10], number=18),
    rule("elig_hk_premier_elite_trb", "acct_hk_premier_elite", "relationship_balance", "gte",
         "过去三个月平均总关系结余须达到7,800,000港元，并持有HSBC Premier。", PRODUCT_BY_ID["acct_hk_premier_elite"][10], number=7800000, currency="HKD"),
    rule("elig_hk_premier_elite_relationship", "acct_hk_premier_elite", "other", "required",
         "申请人须先持有 HSBC Premier Bank Account。", PRODUCT_BY_ID["acct_hk_premier_elite"][10]),
    rule("elig_hk_teens_min_age", "acct_hk_teens", "minimum_age", "gte",
         "申请人须年满11岁。", PRODUCT_BY_ID["acct_hk_teens"][10], number=11),
    rule("elig_hk_teens_max_age", "acct_hk_teens", "maximum_age", "lte",
         "申请人须未满18岁。", PRODUCT_BY_ID["acct_hk_teens"][10], number=17),
    rule("elig_hk_teens_parent", "acct_hk_teens", "existing_customer", "required",
         "须由可使用汇丰香港App的现有客户家长陪同申请。", PRODUCT_BY_ID["acct_hk_teens"][10]),
    rule("elig_hk_teens_hkid", "acct_hk_teens", "other", "required",
         "青少年申请人须持有效香港永久性居民身份证，并持香港出生证明或中国内地出生医学证明。", PRODUCT_BY_ID["acct_hk_teens"][10]),
    rule("elig_hk_teens_location", "acct_hk_teens", "other", "required",
         "申请时须在香港居住并身处香港，且青少年申请人须为汇丰新客户。", PRODUCT_BY_ID["acct_hk_teens"][10]),
    rule("elig_hk_children_max_age", "acct_hk_children_savings", "maximum_age", "lte",
         "申请人须未满18岁。", PRODUCT_BY_ID["acct_hk_children_savings"][10], number=17),
    rule("elig_hk_children_guardian", "acct_hk_children_savings", "other", "required",
         "11至17岁由家长或监护人陪同到网点；11岁以下可由家长以本人名义为孩子开立。", PRODUCT_BY_ID["acct_hk_children_savings"][10]),
    rule("elig_hk_time_deposit_hkd", "acct_hk_time_deposit", "minimum_deposit", "gte",
         "港元定期存款最低金额为10,000港元；其他币种门槛不同。", "https://www.hsbc.com.hk/help/faq/accounts/", number=10000, currency="HKD"),
    rule("elig_hk_time_deposit_relationship", "acct_hk_time_deposit", "existing_customer", "required",
         "须先持有定期存款账户、HSBC One 或 HSBC Premier；新开综合账户后下一工作日可作定期存款。", HK_TIME_DEPOSIT),
    rule("elig_hk_hkd_savings_published", "acct_hk_hkd_savings", "other", "required",
         "开户资格须按香港汇丰个人账户申请要求核对。", HK_ACCOUNTS),
    rule("elig_hk_hkd_current_published", "acct_hk_hkd_current", "other", "required",
         "开户资格须按香港汇丰个人账户申请要求核对。", HK_ACCOUNTS),
    rule("elig_hk_basic_independence_published", "acct_hk_basic_independence", "other", "required",
         "此账户面向心智能力受损客户；须提交已注册的持久授权书、监护令或法院委任委员会等银行接受的法律授权文件。", HK_BASIC_INDEPENDENCE),
]


def document(doc_id, account_id, kind, name, url, *, required=1, condition=None,
             validity=None, route_id=None):
    return (doc_id, account_id, route_id, kind, name, required, None, condition,
            validity, url, VERIFIED_AT)


DOCUMENTS = []
for row in PRODUCTS:
    account_id, _, _, market = row[:4]
    url = row[10]
    if market == "UK":
        required = int(row[4] == "current")
        DOCUMENTS += [
            document(f"doc_{account_id[5:]}_identity", account_id, "identity", "护照、带照片驾照或银行接受的身份证明", url, required=required),
            document(f"doc_{account_id[5:]}_address", account_id, "address", "近三个月账单或银行接受的地址证明", url, required=required),
        ]
    elif market == "US":
        required = int(account_id == "acct_us_premier")
        DOCUMENTS += [
            document(f"doc_{account_id[5:]}_identity", account_id, "identity", "护照、驾照或银行接受的政府签发身份证件", url, required=required),
            document(f"doc_{account_id[5:]}_tax", account_id, "tax", "社会安全号码或税务识别信息", url, required=required),
            document(f"doc_{account_id[5:]}_address", account_id, "address", "美国居住地址及地址证明", url, required=required),
        ]
    else:
        required = int(account_id != "acct_hk_time_deposit")
        DOCUMENTS.append(document(f"doc_{account_id[5:]}_identity", account_id, "identity",
                                  "香港身份证或产品页面接受的其他身份证件", url,
                                  required=required))

DOCUMENTS += [
    document("doc_uk_student_offer_complete", "acct_uk_student", "course_offer", "符合要求的课程录取证明或UCAS状态码", PRODUCT_BY_ID["acct_uk_student"][10]),
    document("doc_uk_graduate_proof", "acct_uk_graduate", "education", "最近两年内毕业的证明", PRODUCT_BY_ID["acct_uk_graduate"][10]),
    document("doc_us_premier_enrollment_complete", "acct_us_premier", "education", "学生申请人可能需要录取或在读证明", PRODUCT_BY_ID["acct_us_premier"][10], required=0),
    document("doc_hk_teens_birth", "acct_hk_teens", "birth", "香港出生证明或中国内地出生医学证明", PRODUCT_BY_ID["acct_hk_teens"][10]),
    document("doc_hk_teens_parent", "acct_hk_teens", "guardian_identity", "家长身份证明及亲子关系资料", PRODUCT_BY_ID["acct_hk_teens"][10]),
    document("doc_hk_children_guardian", "acct_hk_children_savings", "guardian_identity", "家长或监护人身份证明", PRODUCT_BY_ID["acct_hk_children_savings"][10]),
    document("doc_hk_children_birth", "acct_hk_children_savings", "birth", "未满11岁儿童的出生证明或银行接受的儿童身份证明", PRODUCT_BY_ID["acct_hk_children_savings"][10]),
    document("doc_hk_one_selfie", "acct_hk_one", "biometric", "通过App申请时拍摄自拍照进行身份核验", PRODUCT_BY_ID["acct_hk_one"][10]),
    document("doc_hk_one_address", "acct_hk_one", "address", "银行按居住地及个案要求的地址证明或补充文件", HK_OPENING_NOTES, required=0),
    document("doc_hk_premier_address", "acct_hk_premier", "address", "新客户到网点办理时的近期地址证明", PRODUCT_BY_ID["acct_hk_premier"][10]),
    document("doc_hk_hkd_savings_address", "acct_hk_hkd_savings", "address", "银行按居住地及个案要求的近期地址证明", HK_OPENING_NOTES, required=0),
    document("doc_hk_hkd_current_address", "acct_hk_hkd_current", "address", "银行按居住地及个案要求的近期地址证明", HK_OPENING_NOTES, required=0),
    document("doc_hk_basic_authority", "acct_hk_basic_independence", "legal_authority", "已注册的持久授权书、监护令或法院委任委员会等银行接受的法律授权文件", HK_BASIC_INDEPENDENCE),
    document("doc_hk_basic_attorney_identity", "acct_hk_basic_independence", "guardian_identity", "获授权人士的身份证明和地址证明", HK_BASIC_INDEPENDENCE),
]


def fee(fee_id, account_id, kind, description, url, *, amount=None, rate=None,
        currency=None, waiver=0, waiver_condition=None, minimum=None, maximum=None):
    return (fee_id, account_id, kind, amount, rate, currency, minimum, maximum,
            waiver, waiver_condition, description, url, VERIFIED_AT)


FEES = []
for account_id in [r[0] for r in PRODUCTS if r[3] == "UK" and r[4] == "current"]:
    FEES.append(fee(f"fee_{account_id[5:]}_monthly", account_id, "monthly_fee",
                    "英国汇丰当前账户免费开立且不收月费。", UK_CURRENT, amount=0, currency="GBP"))
FEES += [
    fee("fee_us_premier_monthly", "acct_us_premier", "monthly_fee", "未满足任一Premier关系资格时收取月费。", PRODUCT_BY_ID["acct_us_premier"][10], amount=50, currency="USD", waiver=1, waiver_condition="开户当月及随后三个完整日历月豁免；其后满足余额、直接入账、按揭或私人银行资格之一可豁免。"),
    fee("fee_us_new_money_cd_opening", "acct_us_new_money_cd", "minimum_opening_deposit", "New Money CD最低开户存款。", PRODUCT_BY_ID["acct_us_new_money_cd"][10], amount=1000, currency="USD"),
    fee("fee_us_online_cd_opening", "acct_us_online_cd", "minimum_opening_deposit", "Online CD最低开户存款。", PRODUCT_BY_ID["acct_us_online_cd"][10], amount=1000, currency="USD"),
    fee("fee_hk_one_nonhkid", "acct_hk_one", "below_balance_fee", "2026年1月1日起新开户的非香港身份证客户，三个月平均总关系结余低于门槛时收取。", PRODUCT_BY_ID["acct_hk_one"][10], amount=100, currency="HKD", waiver=1, waiver_condition="维持10,000港元总关系结余可免；香港身份证持有人不适用。"),
    fee("fee_hk_premier_below_balance", "acct_hk_premier", "below_balance_fee", "总关系结余低于1,000,000港元时收取。", PRODUCT_BY_ID["acct_hk_premier"][10], amount=380, currency="HKD", waiver=1, waiver_condition="维持1,000,000港元总关系结余可免。"),
    fee("fee_hk_teens_monthly", "acct_hk_teens", "monthly_fee", "HSBC Teens不收月费且无最低结余要求。", PRODUCT_BY_ID["acct_hk_teens"][10], amount=0, currency="HKD"),
    fee("fee_hk_children_monthly", "acct_hk_children_savings", "monthly_fee", "儿童储蓄账户不收账户月费且无最低结余要求。", PRODUCT_BY_ID["acct_hk_children_savings"][10], amount=0, currency="HKD"),
    fee("fee_hk_time_deposit_minimum_hkd", "acct_hk_time_deposit", "minimum_opening_deposit", "港元定期存款最低存入金额。", HK_TIME_DEPOSIT, amount=10000, currency="HKD"),
]


def feature(feature_id, account_id, kind, value, description, url):
    return (feature_id, account_id, kind, 1, value, description, url, VERIFIED_AT)


FEATURES = []
for row in PRODUCTS:
    account_id, _, _, market, account_type = row[:5]
    url = row[10]
    FEATURES.append(feature(f"feature_{account_id[5:]}_digital", account_id, "mobile_banking", "available",
                            "可通过适用的网上银行或手机银行渠道管理。", url))
    if account_type in {"current", "checking", "integrated"}:
        FEATURES.append(feature(f"feature_{account_id[5:]}_payments", account_id, "other", "daily_banking",
                                "可用于相应市场的日常银行服务，具体功能以产品页面为准。", url))
    if "global_money" in account_id or market == "HK" and account_type == "integrated":
        FEATURES.append(feature(f"feature_{account_id[5:]}_multicurrency", account_id, "multi_currency", "available",
                                "支持产品页面列明的多币种服务。", url))


UPSERTS = {
    "account_product": (PRODUCTS, """INSERT INTO account_product
        (account_id,product_code,product_name,market,account_type,customer_segment,primary_currency,
         product_summary,dedicated_student_product,product_status,official_url,data_status,
         effective_from,effective_to,last_verified_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (account_id) DO UPDATE SET product_code=EXCLUDED.product_code,
        product_name=EXCLUDED.product_name,market=EXCLUDED.market,account_type=EXCLUDED.account_type,
        customer_segment=EXCLUDED.customer_segment,primary_currency=EXCLUDED.primary_currency,
        product_summary=EXCLUDED.product_summary,dedicated_student_product=EXCLUDED.dedicated_student_product,
        product_status=EXCLUDED.product_status,official_url=EXCLUDED.official_url,
        data_status=EXCLUDED.data_status,last_verified_at=EXCLUDED.last_verified_at"""),
    "account_opening_route": (ROUTES, """INSERT INTO account_opening_route
        (route_id,account_id,route_name,application_channel,applicant_location,identity_document_type,
         can_apply_before_arrival,branch_visit_required,activation_location,processing_time_note,
         additional_notes,source_url,last_verified_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (route_id) DO UPDATE SET route_name=EXCLUDED.route_name,
        application_channel=EXCLUDED.application_channel,applicant_location=EXCLUDED.applicant_location,
        identity_document_type=EXCLUDED.identity_document_type,
        can_apply_before_arrival=EXCLUDED.can_apply_before_arrival,
        branch_visit_required=EXCLUDED.branch_visit_required,activation_location=EXCLUDED.activation_location,
        processing_time_note=EXCLUDED.processing_time_note,additional_notes=EXCLUDED.additional_notes,
        source_url=EXCLUDED.source_url,last_verified_at=EXCLUDED.last_verified_at"""),
    "account_eligibility_rule": (RULES, """INSERT INTO account_eligibility_rule
        (eligibility_rule_id,account_id,route_id,rule_type,operator,value_text,value_numeric,currency,
         required,description,source_url,last_verified_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (eligibility_rule_id) DO UPDATE SET account_id=EXCLUDED.account_id,
        route_id=EXCLUDED.route_id,rule_type=EXCLUDED.rule_type,operator=EXCLUDED.operator,
        value_text=EXCLUDED.value_text,value_numeric=EXCLUDED.value_numeric,currency=EXCLUDED.currency,
        required=EXCLUDED.required,description=EXCLUDED.description,source_url=EXCLUDED.source_url,
        last_verified_at=EXCLUDED.last_verified_at"""),
    "account_required_document": (DOCUMENTS, """INSERT INTO account_required_document
        (requirement_id,account_id,route_id,document_type,document_name,required,alternative_group,
         condition_note,validity_note,source_url,last_verified_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (requirement_id) DO UPDATE SET account_id=EXCLUDED.account_id,
        route_id=EXCLUDED.route_id,document_type=EXCLUDED.document_type,
        document_name=EXCLUDED.document_name,required=EXCLUDED.required,
        alternative_group=EXCLUDED.alternative_group,condition_note=EXCLUDED.condition_note,
        validity_note=EXCLUDED.validity_note,source_url=EXCLUDED.source_url,
        last_verified_at=EXCLUDED.last_verified_at"""),
    "account_fee": (FEES, """INSERT INTO account_fee
        (fee_id,account_id,fee_type,amount,rate,currency,minimum_amount,maximum_amount,
         waiver_available,waiver_condition,description,source_url,last_verified_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (fee_id) DO UPDATE SET account_id=EXCLUDED.account_id,fee_type=EXCLUDED.fee_type,
        amount=EXCLUDED.amount,rate=EXCLUDED.rate,currency=EXCLUDED.currency,
        minimum_amount=EXCLUDED.minimum_amount,maximum_amount=EXCLUDED.maximum_amount,
        waiver_available=EXCLUDED.waiver_available,waiver_condition=EXCLUDED.waiver_condition,
        description=EXCLUDED.description,source_url=EXCLUDED.source_url,
        last_verified_at=EXCLUDED.last_verified_at"""),
    "account_feature": (FEATURES, """INSERT INTO account_feature
        (feature_id,account_id,feature_type,available,value_text,description,source_url,last_verified_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (feature_id) DO UPDATE SET account_id=EXCLUDED.account_id,
        feature_type=EXCLUDED.feature_type,available=EXCLUDED.available,value_text=EXCLUDED.value_text,
        description=EXCLUDED.description,source_url=EXCLUDED.source_url,
        last_verified_at=EXCLUDED.last_verified_at"""),
    "account_product_relation": (RELATIONS, """INSERT INTO account_product_relation
        (relation_id,source_account_id,target_account_id,relation_type,description,source_url,last_verified_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (relation_id) DO UPDATE SET source_account_id=EXCLUDED.source_account_id,
        target_account_id=EXCLUDED.target_account_id,relation_type=EXCLUDED.relation_type,
        description=EXCLUDED.description,source_url=EXCLUDED.source_url,
        last_verified_at=EXCLUDED.last_verified_at"""),
}


def validate_catalog() -> None:
    ids = [row[0] for row in PRODUCTS]
    codes = [row[1] for row in PRODUCTS]
    if len(ids) != len(set(ids)) or len(codes) != len(set(codes)):
        raise RuntimeError("duplicate product id or code")
    if any(not row[10].startswith("https://") for row in PRODUCTS):
        raise RuntimeError("every product must have an official HTTPS URL")
    expected = {"UK": 16, "US": 6, "HK": 9}
    actual = Counter(row[3] for row in PRODUCTS)
    if dict(actual) != expected:
        raise RuntimeError(f"catalog inventory mismatch: {dict(actual)} != {expected}")
    covered = {row[1] for row in ROUTES}
    if set(ids) - covered:
        raise RuntimeError("products without opening route: " + ", ".join(sorted(set(ids) - covered)))


def main() -> None:
    parser = argparse.ArgumentParser(description="同步汇丰英港美个人存款账户官网目录")
    parser.add_argument("--apply", action="store_true", help="写入RDS；默认只校验并显示计划")
    args = parser.parse_args()
    validate_catalog()
    print("catalog:", dict(Counter(row[3] for row in PRODUCTS)), "total", len(PRODUCTS))
    for table, (rows, _sql) in UPSERTS.items():
        print(f"{table}: {len(rows)} rows")
    if not args.apply:
        print("dry-run only; pass --apply to write RDS")
        return
    connection = storage._connect()
    try:
        connection.execute("SELECT pg_advisory_xact_lock(%s)", (18028,))
        connection.execute(RELATION_DDL)
        with connection.cursor() as cursor:
            for _table, (rows, sql) in UPSERTS.items():
                cursor.executemany(sql, rows)
        ids = [row[0] for row in PRODUCTS]
        counts = connection.execute(
            "SELECT market, COUNT(*) AS n FROM account_product WHERE account_id=ANY(%s) "
            "AND product_status='ACTIVE' GROUP BY market ORDER BY market", (ids,)).fetchall()
        actual = {row["market"]: int(row["n"]) for row in counts}
        if actual != {"HK": 9, "UK": 16, "US": 6}:
            raise RuntimeError(f"post-write verification failed: {actual}")
        connection.commit()
        print("applied and verified:", actual)
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


if __name__ == "__main__":
    main()
