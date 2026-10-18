"""Domain workflow registry."""

from .bank_account import BankAccountWorkflow
from .credit_card import CreditCardWorkflow
from .fx import FxWorkflow
from .visa import VisaWorkflow


def default_domain_workflows(credit_card_repository=None):
    return {
        "visa": VisaWorkflow(),
        "fx": FxWorkflow(),
        "bank_account": BankAccountWorkflow(),
        "credit_card": CreditCardWorkflow(repository=credit_card_repository),
    }


__all__ = ["BankAccountWorkflow", "CreditCardWorkflow", "FxWorkflow", "VisaWorkflow",
           "default_domain_workflows"]
