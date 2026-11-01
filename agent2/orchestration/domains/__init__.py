"""Domain workflow registry."""

from .bank_account import BankAccountWorkflow
from .credit_card import CreditCardWorkflow
from .fx import FxWorkflow
from .visa import VisaWorkflow


def default_domain_workflows(credit_card_repository=None, *, fx_model=None, fx_policy_loader=None,
                             bank_model=None, bank_repository=None,
                             visa_knowledge=None, clock=None, visa_generator=None):
    shared_model = bank_model or fx_model
    return {
        "visa": VisaWorkflow(knowledge=visa_knowledge, clock=clock, generator=visa_generator),
        "fx": FxWorkflow(model=fx_model, policy_loader=fx_policy_loader),
        "bank_account": BankAccountWorkflow(model=shared_model, repository=bank_repository),
        "credit_card": CreditCardWorkflow(repository=credit_card_repository),
    }


__all__ = ["BankAccountWorkflow", "CreditCardWorkflow", "FxWorkflow", "VisaWorkflow",
           "default_domain_workflows"]
