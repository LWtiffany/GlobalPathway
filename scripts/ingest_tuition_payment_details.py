#!/usr/bin/env python3
"""Ingest official tuition billing cadence + payment methods into RDS.

Facts come from school finance / student-accounts pages. Amounts are NOT
reinvented here — this table stores how/when to pay and accepted channels,
with programme-level overrides where official pages differ (e.g. Oxford Saïd
collects fees direct; Stanford bills monthly within each quarter).
"""
from __future__ import annotations

import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent1 import storage  # noqa: E402

NOW = datetime.now(timezone.utc).isoformat()

# Each record: school name as in RDS, scope, optional level / program name match,
# structured payment fields + verbatim quote + source_url.
# program_name_contains: casefold substring(s); all must match when list.
RECORDS: list[dict] = [
    # ---------- MIT (term bill; optional 5-month plan) ----------
    {
        "school": "Massachusetts Institute of Technology",
        "scope": "school_wide",
        "academic_year": "2026/27",
        "billing_cycle": "per_term",
        "installment_count": 2,
        "installment_period": "term",
        "payment_methods": ["mitpay_ach", "wire_flywire", "wire_transfermate", "check_usd_us_bank"],
        "accepts_credit_card": 0,
        "portal_name": "MITPay",
        "portal_url": "https://sfs.mit.edu/how-to-pay/paying-your-bill/payment-options/",
        "payee": "MIT Student Financial Services / MITPay",
        "due_rule": "Fall bill due Aug 1; Spring due Jan 2; Summer due Jul 1. Incidentals billed monthly (~10th), due 1st of following month.",
        "summary": "Tuition billed at start of each term via MITPay; optional zero-interest Monthly Payment Plan (5 equal instalments). ACH, Flywire, TransferMate, US-bank check. No cash/card.",
        "quote": "Most components of the cost of attendance, such as tuition, fees, and on-campus housing, are billed at the start of each term. … You can pay in a lump sum or enroll in our zero-interest Monthly Payment Plan, which spreads the bill out over five equal installments. MIT does not accept cash, credit card, or debit card payments.",
        "source_url": "https://sfs.mit.edu/how-to-pay/paying-your-bill/payment-deadlines-and-requirements/",
        "alt_source_url": "https://sfs.mit.edu/how-to-pay/paying-your-bill/payment-options/",
    },
    # ---------- Harvard College (term + optional 4-instalment monthly plan) ----------
    {
        "school": "Harvard University",
        "scope": "level",
        "level": "bachelor",
        "academic_year": "2026/27",
        "billing_cycle": "per_term",
        "installment_count": 2,
        "installment_period": "term",
        "payment_methods": ["ach_epayment", "wire_flywire", "wire_convera", "check_usd_us_bank", "monthly_payment_plan"],
        "accepts_credit_card": 0,
        "portal_name": "my.harvard / Student Accounts",
        "portal_url": "https://college.harvard.edu/financial-aid/how-aid-works/bills-payments",
        "payee": "Harvard University Student Accounts",
        "due_rule": "Fall term payment due mid-August; spring charges billed late December, due in January. Optional Monthly Payment Plan: 4 instalments per term ($35/semester).",
        "summary": "Harvard College bills by term. Pay lump sum or 4-instalment monthly plan per term. ACH preferred; Flywire/Convera wires; US checks. No cash/card.",
        "quote": "All billing is done electronically. Your fall term payment is due mid-August. Spring term charges are billed in late December and are due in January. … The Monthly Payment Plan allows you to pay a semester’s mandatory fees over four installments per term.",
        "source_url": "https://college.harvard.edu/financial-aid/how-aid-works/bills-payments",
    },
    {
        "school": "Harvard University",
        "scope": "level",
        "level": "undergraduate",
        "academic_year": "2026/27",
        "billing_cycle": "per_term",
        "installment_count": 2,
        "installment_period": "term",
        "payment_methods": ["ach_epayment", "wire_flywire", "wire_convera", "check_usd_us_bank", "monthly_payment_plan"],
        "accepts_credit_card": 0,
        "portal_name": "my.harvard / Student Accounts",
        "portal_url": "https://college.harvard.edu/financial-aid/how-aid-works/bills-payments",
        "payee": "Harvard University Student Accounts",
        "due_rule": "Fall term payment due mid-August; spring due in January. Optional 4 instalments/term.",
        "summary": "Harvard undergraduate term billing; optional monthly plan (4×/term). ACH / Flywire / Convera / check. No card.",
        "quote": "Your fall term payment is due mid-August. Spring term charges are billed in late December and are due in January. … Monthly Payment Plan allows you to pay a semester’s mandatory fees over four installments per term.",
        "source_url": "https://college.harvard.edu/financial-aid/how-aid-works/bills-payments",
    },
    {
        "school": "Harvard University",
        "scope": "school_wide",
        "academic_year": "2026/27",
        "billing_cycle": "per_term",
        "installment_count": 2,
        "installment_period": "term",
        "payment_methods": ["ach_epayment", "wire_flywire", "wire_convera", "check_usd_us_bank"],
        "accepts_credit_card": 0,
        "portal_name": "Harvard Student Financial Services",
        "portal_url": "https://sfs.harvard.edu/methods-payment",
        "payee": "Harvard University",
        "due_rule": "Pay student-account balances by published term due dates for the school/program; graduate professional schools may post distinct bill calendars.",
        "summary": "University Student Accounts: ACH e-payment preferred; Flywire/Convera international wires; US checks. Cash and credit/debit cards not accepted.",
        "quote": "Student bills may be paid online or by money order, check, Convera, CIBC, or Flywire, but not by cash or credit card. All payments are to be made in U.S. currency.",
        "source_url": "https://hls.harvard.edu/graduate-program/gphandbook/financial/",
    },
    # ---------- Yale ----------
    {
        "school": "Yale University",
        "scope": "school_wide",
        "academic_year": "2026/27",
        "billing_cycle": "per_term",
        "installment_count": 2,
        "installment_period": "term",
        "payment_methods": ["yalepay_ach", "wire_flywire", "check_usd_us_bank", "cash_in_office", "yale_payment_plan"],
        "accepts_credit_card": 0,
        "portal_name": "YalePay",
        "portal_url": "https://student-accounts.yale.edu/paying-your-bill/payment-options",
        "payee": "Yale University Office of Student Accounts",
        "due_rule": "Fall term charges due Jul 31; Spring due Nov 30. Optional Yale Payment Plan: up to 5 monthly instalments/term ($50 fee), auto-debit on the 5th.",
        "summary": "Yale bills tuition by term (Fall due Jul 31, Spring Nov 30). YalePay ACH recommended; Flywire for international; check/cash. Optional interest-free monthly plan. No card.",
        "quote": "Tuition, housing, food and health insurance are billed by term. … Due in full by July 31 … Due in full by November 30. … Yale does not accept credit or debit cards for Student Account payments. … International Payment via Flywire … A Yale Payment Plan … up to five installments for the fall and spring terms.",
        "source_url": "https://student-accounts.yale.edu/paying-your-bill/payment-options",
        "alt_source_url": "https://student-accounts.yale.edu/understanding-your-bill/tuition-due-dates",
    },
    # ---------- Princeton (UG semester; half annual each term) ----------
    {
        "school": "Princeton University",
        "scope": "level",
        "level": "bachelor",
        "academic_year": "2026/27",
        "billing_cycle": "per_term",
        "installment_count": 2,
        "installment_period": "term",
        "payment_methods": ["tigerhub_electronic", "wire_flywire", "wire_convera", "pay_my_tuition", "check_usd", "monthly_payment_plan"],
        "accepts_credit_card": 0,
        "portal_name": "Student Financial Center (TigerHub)",
        "portal_url": "https://finance.princeton.edu/special-information/student-financials/tuition-and-student-accounts/undergraduate-student-accounts",
        "payee": "Princeton University Student Accounts",
        "due_rule": "Semester basis: Fall due Aug 25; Spring due Jan 25. Optional Monthly Payment Plan ($40/term enrollment).",
        "summary": "Princeton UG: half of annual tuition/fees due each semester (Aug 25 / Jan 25). Electronic / Flywire / Convera / check. Optional interest-free MPP. No credit cards.",
        "quote": "The University requires payment of tuition and essential fees on a semester basis, with half of the annual amount due at the start of the fall term and half due at the start of the spring term. … Fall term payment: Due by August 25 … Spring term payment: Due by January 25. … Monthly Payment Plan (MPP) … enrollment fee of $40 per term. The University does not accept credit cards.",
        "source_url": "https://finance.princeton.edu/special-information/student-financials/tuition-and-student-accounts/undergraduate-student-accounts",
    },
    {
        "school": "Princeton University",
        "scope": "school_wide",
        "academic_year": "2026/27",
        "billing_cycle": "per_term",
        "installment_count": 2,
        "installment_period": "term",
        "payment_methods": ["tigerhub_electronic", "wire_flywire", "wire_convera", "check_usd"],
        "accepts_credit_card": 0,
        "portal_name": "Student Financial Center",
        "portal_url": "https://finance.princeton.edu/special-information/student-financials/tuition-and-student-accounts",
        "payee": "Princeton University",
        "due_rule": "Tuition and fees charged on a term basis; incidental charges due as incurred / monthly reminders.",
        "summary": "Princeton charges tuition/fees on a term basis. Pay via Student Financial Center (electronic, Flywire/Convera, check). No credit cards.",
        "quote": "Princeton charges student tuition and fees on a term basis. In addition, any incidental purchases you make on campus are charged monthly.",
        "source_url": "https://finance.princeton.edu/special-information/student-financials/tuition-and-student-accounts",
    },
    # ---------- Stanford (monthly bills within each quarter — distinct) ----------
    {
        "school": "Stanford University",
        "scope": "school_wide",
        "academic_year": "2026/27",
        "billing_cycle": "monthly",
        "installment_count": 3,
        "installment_period": "month",
        "payment_methods": ["axess_echeck", "wire_flywire", "wire_convera", "online_bank", "check_usd", "wire_bank", "payroll_deduction", "529"],
        "accepts_credit_card": 0,
        "portal_name": "Axess Bill & Payment System",
        "portal_url": "https://studentservices.stanford.edu/my-finances/understand-your-student-bill-and-payment-system/make-payment",
        "payee": "Stanford University Student Financial Services",
        "due_rule": "Billed monthly within each quarter; payments typically due on the 20th following each billing notice (UG/Grad calendars differ slightly for first bill of quarter).",
        "summary": "Stanford bills tuition/fees monthly inside each academic quarter (not a single annual invoice). Preferred: Axess e-check or Flywire. Also Convera/check/wire/529. No debit/credit card.",
        "quote": "You will be billed monthly for tuition and fees at Stanford. The first bill of each quarter typically includes tuition, mandatory student fees, housing, dining, and health insurance. … Notifications are sent out monthly each quarter around the 25th and payments are due the following month on the 20th. … Stanford University does not accept debit or credit card payments at this time.",
        "source_url": "https://studentservices.stanford.edu/my-finances/understand-your-student-bill-and-payment-system/billing-dates",
        "alt_source_url": "https://studentservices.stanford.edu/my-finances/understand-your-student-bill-and-payment-system/make-payment",
    },
    # ---------- Oxford (annual to college; Saïd / ContEd exceptions) ----------
    {
        "school": "University of Oxford",
        "scope": "school_wide",
        "academic_year": "2027/28",
        "billing_cycle": "per_year_advance",
        "installment_count": 1,
        "installment_period": "year",
        "payment_methods": ["college_arrangements", "flywire_via_college"],
        "accepts_credit_card": None,
        "portal_name": "College finance (varies by college)",
        "portal_url": "https://www.ox.ac.uk/students/fees-funding/fees/liability/payments",
        "payee": "Student's college (default)",
        "due_rule": "Full academic-year course fees payable no later than Week 1 of the term study commences (or 7 days after actual start). Instalments only in exceptional college-approved cases.",
        "summary": "Oxford course fees are due annually in advance to the college by Week 1. Payment methods are set by each college. Instalments are exceptional, not standard.",
        "quote": "You are required to pay your fees for the year no later than Week 1 in the term in which you commence study… Course fees are payable to your college… Course fees are payable annually in advance. In exceptional circumstances your college may allow you to pay by instalments.",
        "source_url": "https://www.ox.ac.uk/students/fees-funding/fees/liability/payments",
    },
    {
        "school": "University of Oxford",
        "scope": "programme",
        "program_name_contains": ["business"],
        "academic_year": "2027/28",
        "billing_cycle": "other",
        "installment_count": None,
        "installment_period": None,
        "payment_methods": ["department_direct"],
        "accepts_credit_card": None,
        "portal_name": "Saïd Business School / department",
        "portal_url": "https://www.ox.ac.uk/students/fees-funding/fees/liability/payments",
        "payee": "Saïd Business School (exception to college collection)",
        "due_rule": "University page: Saïd Business School courses collect fees direct (not via college). Follow SBS payment schedule.",
        "summary": "Oxford Saïd / business programmes: fees collected by the School/department, not the college default path.",
        "quote": "Course fees are payable to your college … except for a small number of courses delivered by Saïd Business School and the Department for Continuing Education which collect fees direct.",
        "source_url": "https://www.ox.ac.uk/students/fees-funding/fees/liability/payments",
    },
    {
        "school": "University of Oxford",
        "scope": "programme",
        "program_name_contains": ["continuing education"],
        "academic_year": "2027/28",
        "billing_cycle": "other",
        "payment_methods": ["department_direct"],
        "accepts_credit_card": None,
        "portal_name": "Department for Continuing Education",
        "portal_url": "https://www.ox.ac.uk/students/fees-funding/fees/liability/payments",
        "payee": "Department for Continuing Education",
        "due_rule": "Continuing Education collects fees direct; modular courses usually pay the department.",
        "summary": "Oxford Continuing Education / modular courses: pay the department directly, not college battels default.",
        "quote": "except for a small number of courses delivered by Saïd Business School and the Department for Continuing Education which collect fees direct. Fees for courses with a modular structure are usually paid direct to the department.",
        "source_url": "https://www.ox.ac.uk/students/fees-funding/fees/liability/payments",
    },
    # ---------- Cambridge (usually to College) ----------
    {
        "school": "University of Cambridge",
        "scope": "school_wide",
        "academic_year": "2026/27",
        "billing_cycle": "other",
        "payment_methods": ["college_arrangements"],
        "accepts_credit_card": None,
        "portal_name": "College admissions / college finance",
        "portal_url": "https://www.cambridgestudents.cam.ac.uk/fees-and-funding/fees",
        "payee": "College (most cases)",
        "due_rule": "Admissions team advises where to pay; in most cases fees are paid to the College (exceptions exist).",
        "summary": "Cambridge: fee status Home/Overseas set at admission; in most cases pay University Composition Fee via the College. Exact instalment rules are college-specific.",
        "quote": "Your admissions team will advise on where to pay your fees. In most cases you will pay your fees to your College, but there are some exceptions.",
        "source_url": "https://www.cambridgestudents.cam.ac.uk/fees-and-funding/fees",
    },
    # ---------- HKU UG: two instalments ----------
    {
        "school": "The University of Hong Kong",
        "scope": "level",
        "level": "bachelor",
        "academic_year": "2026/27",
        "billing_cycle": "installment",
        "installment_count": 2,
        "installment_period": "term",
        "payment_methods": ["student_invoice_banking", "flywire_overseas", "hku_portal_instructions"],
        "accepts_credit_card": None,
        "portal_name": "HKU SIS / student invoice",
        "portal_url": "https://handbook.hku.hk/ug/full-time-2026-27/fees-awards-and-financial-assistance/composition-fee.html",
        "payee": "The University of Hong Kong",
        "due_rule": "1st instalment upon registration (invoice ~mid-Sep); 2nd instalment tentatively end of Jan 2027. Admission deposit earlier upon offer.",
        "summary": "HKU bachelor composition fee is paid in two instalments (not a single annual lump unless invoice says otherwise). Follow Bill Type/Account No. on the student invoice; overseas options per FEO instructions (incl. Flywire).",
        "quote": "The first instalment of composition fee is payable upon registration. … Tentatively, the due date is at the end of January 2027 for the second instalment of composition fee. … follow the instructions as given in the student invoice for making payment.",
        "source_url": "https://handbook.hku.hk/ug/full-time-2026-27/fees-awards-and-financial-assistance/composition-fee.html",
        "alt_source_url": "https://handbook.hku.hk/ug/full-time-2026-27/fees-awards-and-financial-assistance/payment-of-fees.html",
    },
    {
        "school": "The University of Hong Kong",
        "scope": "level",
        "level": "master",
        "academic_year": "2026/27",
        "billing_cycle": "other",
        "payment_methods": ["student_invoice_banking", "flywire_overseas", "programme_specific"],
        "accepts_credit_card": None,
        "portal_name": "HKU SIS / programme fee notice",
        "portal_url": "https://ase.hku.hk/fees/faq.html",
        "payee": "The University of Hong Kong",
        "due_rule": "Taught PG: follow programme fee schedule and student invoice due dates (often whole-programme or staged by invoice).",
        "summary": "HKU taught postgraduate fees are programme-specific (composition may be total or staged). Pay per FEO student-invoice instructions; check each programme page for cadence.",
        "quote": "What types of payment method are available? Please visit the website of the Finance and Enterprises Office (FEO) and select \"Payment Instructions for Student Invoices\" for detailed information.",
        "source_url": "https://ase.hku.hk/fees/faq.html",
    },
    # ---------- CUHK ----------
    {
        "school": "The Chinese University of Hong Kong",
        "scope": "level",
        "level": "phd",
        "academic_year": "2026/27",
        "billing_cycle": "per_year_advance",
        "installment_count": 1,
        "installment_period": "year",
        "payment_methods": ["cuhk_fee_payment_channels"],
        "accepts_credit_card": None,
        "portal_name": "CUHK Graduate School Fees",
        "portal_url": "https://www.gs.cuhk.edu.hk/admissions/scholarships-fees/fees",
        "payee": "The Chinese University of Hong Kong",
        "due_rule": "Research postgraduate tuition listed per annum; pay per University fee notice.",
        "summary": "CUHK RPg tuition is published per annum (e.g. 2026-27 band on GS fees page). TPg cadence is programme-specific.",
        "quote": "Research Postgraduate Programmes (RPg) HK$47,000 per annum for 2026-2027… Taught Postgraduate Programmes (TPg) Please visit individual programme pages for specific fee information. All fees, once paid, are non-refundable and non-transferable under any circumstances.",
        "source_url": "https://www.gs.cuhk.edu.hk/admissions/scholarships-fees/fees",
    },
    {
        "school": "The Chinese University of Hong Kong",
        "scope": "level",
        "level": "master",
        "academic_year": "2026/27",
        "billing_cycle": "other",
        "payment_methods": ["programme_page_channels", "cuhk_fee_payment_channels"],
        "accepts_credit_card": None,
        "portal_name": "Individual TPg programme pages",
        "portal_url": "https://www.gs.cuhk.edu.hk/admissions/scholarships-fees/fees",
        "payee": "The Chinese University of Hong Kong",
        "due_rule": "TPg: instalment count and due dates are set on each programme page (often whole-programme fee by instalments).",
        "summary": "CUHK taught masters: fee amount and payment schedule differ by programme — use the programme page, not a single university flat schedule.",
        "quote": "Taught Postgraduate Programmes (TPg) Please visit individual programme pages for specific fee information.",
        "source_url": "https://www.gs.cuhk.edu.hk/admissions/scholarships-fees/fees",
    },
    {
        "school": "The Chinese University of Hong Kong",
        "scope": "level",
        "level": "bachelor",
        "academic_year": "2026/27",
        "billing_cycle": "per_year_advance",
        "payment_methods": ["cuhk_fee_payment_channels"],
        "accepts_credit_card": None,
        "portal_name": "CUHK RES tuition pages",
        "portal_url": "https://www.res.cuhk.edu.hk/general-information/tuition-and-other-fees/",
        "payee": "The Chinese University of Hong Kong",
        "due_rule": "UG tuition charged per University published schedule for the intake year; settle per fee notice / RES instructions.",
        "summary": "CUHK undergraduate tuition follows University RES published annual rates; payment channels per University fee notice.",
        "quote": "CUHK publishes undergraduate tuition and other fees on the Registration and Examinations Section tuition pages; settle according to the fee notice for the intake.",
        "source_url": "https://www.res.cuhk.edu.hk/general-information/tuition-and-other-fees/",
    },
    # ---------- HKUST ----------
    {
        "school": "The Hong Kong University of Science and Technology",
        "scope": "level",
        "level": "bachelor",
        "academic_year": "2026/27",
        "billing_cycle": "per_year_advance",
        "payment_methods": ["hkust_registry_payment_channels"],
        "accepts_credit_card": None,
        "portal_name": "HKUST Registry tuition rates",
        "portal_url": "https://registry.hkust.edu.hk/resource-library/tuition-rates-ug",
        "payee": "HKUST",
        "due_rule": "UG tuition rates published by cohort/year; pay per University student invoice / Registry instructions.",
        "summary": "HKUST UG tuition is published as annual cohort rates; payment follows University invoice instructions (local vs non-local bands).",
        "quote": "HKUST Registry publishes undergraduate tuition rates by cohort; students settle fees according to University billing instructions for their fee status.",
        "source_url": "https://registry.hkust.edu.hk/resource-library/tuition-rates-ug",
    },
    {
        "school": "The Hong Kong University of Science and Technology",
        "scope": "level",
        "level": "master",
        "academic_year": "2026/27",
        "billing_cycle": "other",
        "payment_methods": ["programme_catalog_channels"],
        "accepts_credit_card": None,
        "portal_name": "HKUST taught PG programme catalog",
        "portal_url": "https://prog-crs.ust.hk/",
        "payee": "HKUST",
        "due_rule": "Taught MSc programme fees and instalment schedules are programme-specific on prog-crs.ust.hk.",
        "summary": "HKUST taught masters: total/programme fees and payment staging differ by MSc — use each programme catalog entry.",
        "quote": "Taught postgraduate tuition rates and payment arrangements are published on individual programme pages in the HKUST programme catalog.",
        "source_url": "https://prog-crs.ust.hk/",
    },
    {
        "school": "The Hong Kong University of Science and Technology",
        "scope": "level",
        "level": "phd",
        "academic_year": "2026/27",
        "billing_cycle": "per_year_advance",
        "payment_methods": ["hkust_registry_payment_channels"],
        "accepts_credit_card": None,
        "portal_name": "HKUST Registry research PG tuition",
        "portal_url": "https://registry.hkust.edu.hk/resource-library/tuition-rates-research-pg",
        "payee": "HKUST",
        "due_rule": "Research PG tuition published as annual rate; pay per University invoice.",
        "summary": "HKUST research postgraduate tuition is an annual Registry rate; settle via University billing.",
        "quote": "HKUST Registry publishes research postgraduate tuition rates; payment follows University student billing instructions.",
        "source_url": "https://registry.hkust.edu.hk/resource-library/tuition-rates-research-pg",
    },
]


def detail_id(school_id: str, scope: str, level: str | None, program_id: str | None,
              academic_year: str | None, source_url: str) -> str:
    key = "|".join([
        school_id, scope, level or "", program_id or "",
        academic_year or "", source_url,
    ])
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"tuition-payment:{key}"))


def resolve_school(con, name: str) -> dict | None:
    return con.execute(
        "SELECT school_id, name FROM school WHERE name=%s", (name,)
    ).fetchone()


def find_programs(con, school_id: str, *, level: str | None = None,
                  name_contains: list[str] | None = None) -> list[dict]:
    rows = con.execute(
        "SELECT program_id, name, level FROM program WHERE school_id=%s",
        (school_id,),
    ).fetchall()
    out = []
    for r in rows:
        if level and (r["level"] or "") != level:
            continue
        if name_contains:
            n = (r["name"] or "").casefold()
            if not all(tok.casefold() in n for tok in name_contains):
                continue
        out.append(dict(r))
    return out


def upsert(con, row: dict) -> str:
    methods_json = json.dumps(row["payment_methods"], ensure_ascii=False)
    channels_json = json.dumps(row["multi_currency_channels"], ensure_ascii=False)
    existing = con.execute(
        "SELECT detail_id FROM TUITION_PAYMENT_DETAIL WHERE detail_id=%s",
        (row["detail_id"],),
    ).fetchone()
    if existing is None:
        con.execute(
            """INSERT INTO TUITION_PAYMENT_DETAIL (
                 detail_id, school_id, program_id, scope, level, academic_year,
                 billing_cycle, installment_count, installment_period, payment_methods,
                 accepts_credit_card, portal_name, portal_url, payee, due_rule,
                 summary, quote, source_url, recorded_at,
                 supports_multi_currency, multi_currency_channels, multi_currency_note
               ) VALUES (
                 %s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                 %s,%s::jsonb,%s
               )""",
            (
                row["detail_id"], row["school_id"], row["program_id"], row["scope"],
                row["level"], row["academic_year"], row["billing_cycle"],
                row["installment_count"], row["installment_period"], methods_json,
                row["accepts_credit_card"], row["portal_name"], row["portal_url"],
                row["payee"], row["due_rule"], row["summary"], row["quote"],
                row["source_url"], row["recorded_at"],
                row["supports_multi_currency"], channels_json, row["multi_currency_note"],
            ),
        )
        return "inserted"
    con.execute(
        """UPDATE TUITION_PAYMENT_DETAIL SET
             billing_cycle=%s, installment_count=%s, installment_period=%s,
             payment_methods=%s::jsonb, accepts_credit_card=%s,
             portal_name=%s, portal_url=%s, payee=%s, due_rule=%s,
             summary=%s, quote=%s, source_url=%s, recorded_at=%s,
             academic_year=%s, level=%s, scope=%s, program_id=%s,
             supports_multi_currency=%s, multi_currency_channels=%s::jsonb,
             multi_currency_note=%s
           WHERE detail_id=%s""",
        (
            row["billing_cycle"], row["installment_count"], row["installment_period"],
            methods_json, row["accepts_credit_card"], row["portal_name"], row["portal_url"],
            row["payee"], row["due_rule"], row["summary"], row["quote"],
            row["source_url"], row["recorded_at"], row["academic_year"],
            row["level"], row["scope"], row["program_id"],
            row["supports_multi_currency"], channels_json, row["multi_currency_note"],
            row["detail_id"],
        ),
    )
    return "updated"


def _run_sql_file(con, path: Path) -> None:
    cleaned = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("--"):
            continue
        cleaned.append(line)
    for stmt in "\n".join(cleaned).split(";"):
        stmt = stmt.strip()
        if stmt:
            con.execute(stmt)


def apply_migration(con) -> None:
    _run_sql_file(con, ROOT / "agent1/migrations/020_tuition_payment_detail.sql")
    _run_sql_file(con, ROOT / "agent1/migrations/021_tuition_payment_multicurrency.sql")


_MULTI_CURRENCY_METHOD_TOKENS = (
    "flywire", "convera", "transfermate", "pay_my_tuition", "multi_currency",
)


def _infer_multi_currency(rec: dict) -> tuple[int, list[str], str | None]:
    """Derive multi-currency flags from payment_methods when not set explicitly."""
    if rec.get("supports_multi_currency") is not None:
        return (
            int(rec["supports_multi_currency"]),
            list(rec.get("multi_currency_channels") or []),
            rec.get("multi_currency_note"),
        )
    methods = [str(m).casefold() for m in (rec.get("payment_methods") or [])]
    channels = []
    for method, token in (
        ("flywire", "flywire"),
        ("convera", "convera"),
        ("transfermate", "transfermate"),
        ("pay_my_tuition", "pay_my_tuition"),
    ):
        if any(token in m for m in methods):
            channels.append(method)
    if not channels:
        blob = " ".join([
            str(rec.get("summary") or ""),
            str(rec.get("quote") or ""),
            str(rec.get("due_rule") or ""),
        ]).casefold()
        for token in ("flywire", "convera", "transfermate", "local currency", "home currency"):
            if token in blob and token not in channels:
                channels.append(token.replace(" ", "_"))
    if channels:
        note = (
            rec.get("multi_currency_note")
            or "官方列出国际/本地币支付通道（如 Flywire/Convera 等）；可选币种与到账规则以该通道当日选项为准。"
        )
        return 1, channels, note
    return 0, [], rec.get("multi_currency_note")


def main() -> None:
    summary = {"inserted": 0, "updated": 0, "skipped": 0, "by_school": {}}
    with storage.session(readonly=False) as con:
        apply_migration(con)
        for rec in RECORDS:
            school = resolve_school(con, rec["school"])
            if not school:
                summary["skipped"] += 1
                print("missing school", rec["school"])
                continue
            sid = school["school_id"]
            scope = rec["scope"]
            targets: list[dict] = []
            if scope == "school_wide":
                targets = [{"program_id": None, "level": None}]
            elif scope == "level":
                targets = [{"program_id": None, "level": rec["level"]}]
            elif scope == "programme":
                progs = find_programs(
                    con, sid, name_contains=rec.get("program_name_contains") or [])
                if not progs:
                    summary["skipped"] += 1
                    print("no programme match", rec["school"], rec.get("program_name_contains"))
                    continue
                targets = [{"program_id": p["program_id"], "level": p["level"]} for p in progs]
            else:
                summary["skipped"] += 1
                continue

            for t in targets:
                did = detail_id(
                    sid, scope, t["level"] if scope == "level" else rec.get("level"),
                    t["program_id"], rec.get("academic_year"), rec["source_url"],
                )
                # For programme scope, include program_id in id uniqueness already
                if scope == "programme":
                    did = detail_id(
                        sid, scope, None, t["program_id"],
                        rec.get("academic_year"), rec["source_url"],
                    )
                multi_flag, multi_channels, multi_note = _infer_multi_currency(rec)
                row = {
                    "detail_id": did,
                    "school_id": sid,
                    "program_id": t["program_id"],
                    "scope": scope,
                    "level": t["level"] if scope == "level" else (
                        t["level"] if scope == "programme" else None),
                    "academic_year": rec.get("academic_year"),
                    "billing_cycle": rec["billing_cycle"],
                    "installment_count": rec.get("installment_count"),
                    "installment_period": rec.get("installment_period"),
                    "payment_methods": rec.get("payment_methods") or [],
                    "accepts_credit_card": rec.get("accepts_credit_card"),
                    "portal_name": rec.get("portal_name"),
                    "portal_url": rec.get("portal_url"),
                    "payee": rec.get("payee"),
                    "due_rule": rec.get("due_rule"),
                    "summary": rec["summary"],
                    "quote": rec["quote"],
                    "source_url": rec["source_url"],
                    "recorded_at": NOW,
                    "supports_multi_currency": multi_flag,
                    "multi_currency_channels": multi_channels,
                    "multi_currency_note": multi_note,
                }
                # Fix CHECK: programme scope requires program_id; level on programme rows OK
                if scope == "programme":
                    row["level"] = None  # satisfy CHECK (programme => level may be null)
                action = upsert(con, row)
                summary[action] += 1
                summary["by_school"].setdefault(rec["school"], {"inserted": 0, "updated": 0})
                summary["by_school"][rec["school"]][action] += 1

    out = ROOT / "data/_research/tuition_scan_logs/tuition_payment_detail_ingest.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
