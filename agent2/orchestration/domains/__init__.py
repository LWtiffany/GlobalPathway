from .bank_account import BankAccountWorkflow
from .credit_card import CreditCardWorkflow
from .fx import FxWorkflow
from .visa import VisaWorkflow


def default_domain_workflows():
    return {
        "visa": VisaWorkflow(),
        "fx": FxWorkflow(),
        "bank_account": BankAccountWorkflow(),
        "credit_card": CreditCardWorkflow(),
    }


__all__ = ["default_domain_workflows"]
