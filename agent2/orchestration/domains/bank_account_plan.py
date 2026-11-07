"""Evidence-scoped actions for one profile's account-opening task."""
from __future__ import annotations

import re
from agent2.models import AnswerBody
from .bank_account_answer import MARKET_LABEL


def build_opening_plan(*, profile_id: str, market: str, facts: dict, context: dict,
                       results: list[dict], text: str, previous: dict) -> tuple[dict, AnswerBody]:
    location = facts.get("application_location")
    location_label = {"CN": "中国内地", "CN-HK": "香港", "UK": "英国", "US": "美国"}.get(location)
    subject = "孩子" if facts.get("account_applicant_relation") == "child" else "申请人"
    destination = MARKET_LABEL[market]
    preparing = bool(re.search(r"准备|材料|带什么", text))
    handoff = bool(re.search(r"转给|交接|发给|摘要|帮我联系|帮我预约", text))
    schedule = bool(re.search(r"多久|来得及|两周|时间|拿卡|收卡", text))
    fees = bool(re.search(r"另放|资金|存款|月费|豁免|免.*费", text))
    stage = "preparing" if location else "clarifying_location"
    if location:
        stage = "preparing"
    if handoff:
        stage = "handoff_ready"
    if fees:
        stage = "relationship_check"
    if schedule:
        stage = "timeline_check"
    evidence_ids = sorted({e["evidence_id"] for r in results for e in r.get("evidence", [])})
    routes = [r for r in results if r["status"] != "does_not_meet_published_criteria"]
    published = [r for r in routes if r.get("predeparture_stage") == "application_only"]
    checks = [f"{subject}从{location_label or '当前所在地'}申请{destination}账户的渠道及本人到场要求",
              "家长现有关系下是否有适用的家庭服务，以及是否涉及费用条件",
              "最终材料清单、收卡地点和激活步骤；哪些能在出发前完成"]
    actions = [
        {"action_id": "verify_route", "owner": "relationship_manager",
         "status": "needs_bank_confirmation", "depends_on": ["application_location"],
         "text": checks[0], "evidence_ids": evidence_ids},
        {"action_id": "prepare_documents", "owner": "applicant", "status": "ready",
         "depends_on": [], "text": "整理已有身份证明、常住地址证明和留学出发安排；正式清单待渠道确认",
         "evidence_ids": evidence_ids},
        {"action_id": "confirm_fulfilment", "owner": "relationship_manager",
         "status": "needs_bank_confirmation", "depends_on": ["verify_route"],
         "text": checks[2], "evidence_ids": evidence_ids},
    ]
    relation = context.get("parent_relationship_statement")
    relationship_record = ({"statement": relation,
                            "market": context.get("relationship_market", "unknown"),
                            "status": context.get("relationship_status", "user_stated_unverified")}
                           if relation else None)
    known = {"applicant": subject, "destination_market": market,
             "application_location": location, "applicant_age": facts.get("applicant_age"),
             "parent_relationship": relationship_record,
             "relationship_status": context.get("relationship_status", "unknown"),
             "target": context.get("target_milestone") or context.get("customer_goal")}
    plan = {"schema_version": "account-opening-plan-1", "profile_id": profile_id,
            "market": market, "stage": stage, "known_facts": known,
            "path_results": results, "actions": actions, "bank_checks": checks,
            "next_question": None if location else "application_location",
            "handoff": {"status": "draft", "sent": False, "known_facts": known, "questions": checks},
            "revision": int(previous.get("revision") or 0) + 1}
    relationship_market = {"CN-HK": "香港", "UK": "英国", "US": "美国"}.get(
        context.get("relationship_market"), "尚未明确所在地的")
    relationship = f"您提到的{relationship_market}银行关系已记入准备摘要，下一步可请原客户经理核对适用的家庭服务。"
    route = (f"已核对到{destination}账户的境外申请入口，可以先查申请资格；"
             "这不代表已确认您所在地区可以完成开户、收卡和激活。" if published else
             f"先核对{subject}从当前所在地申请{destination}账户的具体渠道，再确定材料和办理节点。")
    location_step = (f"{subject}目前在{location_label}；请客户经理先确认从这里提交是否可行、是否需要本人到场。"
                     if location else "先明确准备申请时人在哪里，这会决定可用渠道及是否需要到场。")
    materials = "现在可以先整理已有的身份证明、常住地址证明，以及留学地区和出发安排；地址证明的格式和有效期按最终渠道核对。"
    fulfilment = "申请提交、银行审核、收卡和激活分开确认；请客户经理逐项回复可完成的时间和地点，再决定出发前与落地后的安排。"
    if handoff:
        summary = "下面这份摘要可以交给您的客户经理，目前尚未发送或预约。"
        age = facts.get("applicant_age")
        steps = [f"开户安排：{subject}{str(age) + '岁，' if age is not None else '，'}拟办理{destination}账户；"
                 f"申请所在地为{location_label or '待确认'}。",
                 "现有关系：" + (relation + "（用户陈述，待银行核实）。" if relation else "尚未说明。"),
                 "目标：" + str(known["target"] or "出发前明确开户及后续安排"),
                 "请客户经理核对：" + "；".join(checks) + "。"]
    elif fees:
        summary = "现在还不能仅凭家长的银行关系确定孩子需要另放多少资金，或是否能免除费用。"
        steps = [relationship, "开户资格、持续关系余额和月费豁免是不同条件；先核对孩子适用的账户及家庭服务路径，再计算本人需要维持的资金。",
                 location_step, "给客户经理的核对重点是：关系是否可关联到孩子、适用哪个实体，以及豁免是否另有年龄、地址或余额条件。"]
    elif schedule:
        summary = "可以按出发日期倒排准备，但目前没有足够依据承诺出发前一定拿到卡。"
        steps = [route, location_step, materials, fulfilment]
    elif preparing:
        summary = "先把已有资料和待确认问题整理好，客户经理就能直接核对办理路径，减少您来回补充。"
        steps = [location_step, materials, relationship, fulfilment]
    else:
        summary = f"可以先按“{subject}赴{destination}、出发前安排好账户”推进：先核对境外渠道，再落实材料、收卡和启用。"
        steps = [route, relationship, location_step, materials]
    return plan, AnswerBody(summary=summary, steps=steps,
                            limitations=["具体资格和渠道需按申请人个案核实；尚未提交申请或作出银行办理承诺。"])
