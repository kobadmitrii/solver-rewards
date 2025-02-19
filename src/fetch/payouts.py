"""Logic for Post CIP 20 Solver Payout Calculation"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import timedelta

from typing import Callable

import pandas
from dune_client.types import Address
from pandas import DataFrame, Series

from src.constants import COW_TOKEN_ADDRESS
from src.fetch.dune import DuneFetcher
from src.fetch.prices import eth_in_token, TokenId, token_in_eth
from src.models.accounting_period import AccountingPeriod
from src.models.overdraft import Overdraft
from src.models.token import Token
from src.models.transfer import Transfer
from src.pg_client import MultiInstanceDBFetcher
from src.utils.print_store import Category

PERIOD_BUDGET_COW = 383307 * 10**18

PAYMENT_COLUMNS = {
    "solver",
    "payment_eth",
    "execution_cost_eth",
    "secondary_reward_eth",
    "reward_cow",
    "secondary_reward_cow",
}
SLIPPAGE_COLUMNS = {
    "solver",
    "solver_name",
    "eth_slippage_wei",
}
REWARD_TARGET_COLUMNS = {"solver", "reward_target"}

COMPLETE_COLUMNS = PAYMENT_COLUMNS.union(SLIPPAGE_COLUMNS).union(REWARD_TARGET_COLUMNS)
NUMERICAL_COLUMNS = [
    "payment_eth",
    "execution_cost_eth",
    "reward_eth",
    "reward_cow",
    "secondary_reward_cow",
    "secondary_reward_eth",
]


@dataclass
class PeriodPayouts:
    """Dataclass to keep track of reimbursements, rewards and solver overdrafts"""

    overdrafts: list[Overdraft]
    # ETH Reimbursements & COW Rewards
    transfers: list[Transfer]


class RewardAndPenaltyDatum:  # pylint: disable=too-many-instance-attributes
    """
    All pertinent information and functionality related to individual solver payout (or overdraft)
    """

    def __init__(  # pylint: disable=too-many-arguments
        self,
        solver: Address,
        solver_name: str,
        reward_target: Address,
        exec_cost: int,
        payment_eth: int,
        secondary_reward_eth: int,
        slippage_eth: int,
        primary_reward_cow: int,
        secondary_reward_cow: int,
    ):
        assert exec_cost >= 0, f"invalid execution cost {exec_cost}"
        assert secondary_reward_eth >= 0, "invalid secondary_reward_eth"
        assert secondary_reward_cow >= 0, "invalid secondary_reward_cow"

        self.solver = solver
        self.solver_name = solver_name
        self.reward_target = reward_target
        self.exec_cost = exec_cost
        self.payment_eth = payment_eth
        self.slippage_eth = slippage_eth
        self.primary_reward_cow = primary_reward_cow
        self.secondary_reward_eth = secondary_reward_eth
        self.secondary_reward_cow = secondary_reward_cow

    @classmethod
    def from_series(cls, frame: Series) -> RewardAndPenaltyDatum:
        """Constructor from row in Dataframe"""
        slippage = (
            int(frame["eth_slippage_wei"])
            if not math.isnan(frame["eth_slippage_wei"])
            else 0
        )
        solver = frame["solver"]
        reward_target = frame["reward_target"]
        if reward_target is None:
            logging.warning(f"solver {solver} without reward_target. Using solver")
            reward_target = solver

        return cls(
            solver=Address(solver),
            solver_name=frame["solver_name"],
            reward_target=Address(reward_target),
            payment_eth=int(frame["payment_eth"]),
            slippage_eth=slippage,
            primary_reward_cow=int(frame["reward_cow"]),
            exec_cost=int(frame["execution_cost_eth"]),
            secondary_reward_eth=int(frame["secondary_reward_eth"]),
            secondary_reward_cow=int(frame["secondary_reward_cow"]),
        )

    def total_outgoing_eth(self) -> int:
        """Total outgoing amount (including slippage) for the payout."""
        return self.payment_eth + self.secondary_reward_eth + self.slippage_eth

    def total_cow_reward(self) -> int:
        """Total outgoing COW token reward"""
        return self.primary_reward_cow + self.secondary_reward_cow

    def total_eth_reward(self) -> int:
        """Total outgoing ETH reward"""
        return self.payment_eth - self.exec_cost + self.secondary_reward_eth

    def is_overdraft(self) -> bool:
        """
        True if the solver's complete combined data results in a net negative
        """
        return self.total_outgoing_eth() < 0

    def as_payouts(self) -> list[Transfer]:
        """
        Isolating the logic of how solvers are paid out according to their
            execution costs, rewards and slippage
        """
        # We do not handle overdraft scenario here!
        assert not self.is_overdraft()
        total_eth_reward = int(self.total_eth_reward())
        total_cow_reward = int(self.total_cow_reward())

        reimbursement_eth = int(self.exec_cost + self.slippage_eth)
        # We do not have access to token conversion here, but we do have other converted values
        # x_eth:x_cow = y_eth:y_cow --> y_cow = y_eth * x_cow / x_eth
        reimbursement_cow = (
            (reimbursement_eth * total_cow_reward) // total_eth_reward
            if total_eth_reward != 0
            else 0
        )

        if reimbursement_eth > 0 > total_cow_reward:
            # If the total payment is positive but the total rewards are negative,
            # pay the total payment in ETH. The total payment corresponds to reimbursement,
            # reduced by the negative reward.
            # Note that;
            # reimbursement_eth + reward_eth
            # = self.total_eth_reward() + self.exec_cost + self.slippage_eth
            # = self.payment_eth + self.secondary_reward_eth + self.slippage_eth
            # = self.total_outgoing_eth()
            # >= 0 (because not self.is_overdraft())
            return (
                [
                    Transfer(
                        token=None,
                        recipient=self.solver,
                        amount_wei=reimbursement_eth + total_eth_reward,
                    )
                ]
                if reimbursement_eth + total_eth_reward > 0
                else []
            )

        if reimbursement_eth < 0 < total_cow_reward:
            # If the total payment is positive but the total reimbursement is negative,
            # pay the total payment in COW. The total payment corresponds to a payment of rewards,
            # reduced by the negative reimbursement.
            return (
                [
                    Transfer(
                        token=Token(COW_TOKEN_ADDRESS),
                        recipient=self.reward_target,
                        amount_wei=reimbursement_cow + total_cow_reward,
                    )
                ]
                if reimbursement_cow + total_cow_reward > 0
                else []
            )

        result = []
        try:
            result.append(
                Transfer(
                    token=None,
                    recipient=self.solver,
                    amount_wei=reimbursement_eth,
                )
            )
        except AssertionError:
            logging.warning(
                f"Invalid ETH Transfer {self.solver} with amount={reimbursement_eth}"
            )
        try:
            result.append(
                Transfer(
                    token=Token(COW_TOKEN_ADDRESS),
                    recipient=self.reward_target,
                    amount_wei=total_cow_reward,
                )
            )
        except AssertionError:
            logging.warning(
                f"Invalid COW Transfer {self.solver} with amount={total_cow_reward}"
            )

        return result


@dataclass
class TokenConversion:
    """
    Data Structure containing token conversion methods.
    """

    eth_to_token: Callable
    token_to_eth: Callable


def extend_payment_df(pdf: DataFrame, converter: TokenConversion) -> DataFrame:
    """
    Extending the basic columns returned by SQL Query with some after-math:
    - reward_eth as difference of payment and execution_cost
    - reward_cow as conversion from ETH to cow.
    - secondary_reward (as the remaining reward after all has been distributed)
        This is evaluated in both ETH and COW (for different use cases).
    """
    # Note that this can be negative!
    pdf["reward_eth"] = pdf["payment_eth"] - pdf["execution_cost_eth"]
    pdf["reward_cow"] = pdf["reward_eth"].apply(converter.eth_to_token)

    secondary_allocation = max(PERIOD_BUDGET_COW - pdf["reward_cow"].sum(), 0)
    participation_total = pdf["num_participating_batches"].sum()
    pdf["secondary_reward_cow"] = (
        secondary_allocation * pdf["num_participating_batches"] / participation_total
    )
    pdf["secondary_reward_eth"] = pdf["secondary_reward_cow"].apply(
        converter.token_to_eth
    )

    for number_col in NUMERICAL_COLUMNS:
        pdf[number_col] = pandas.to_numeric(pdf[number_col])

    return pdf


def prepare_transfers(payout_df: DataFrame, period: AccountingPeriod) -> PeriodPayouts:
    """
    Manipulates the payout DataFrame to split into ETH and COW.
    Specifically, We deduct total_rewards by total_execution_cost (both initially in ETH)
    keep the execution cost in ETH and convert the difference to COW.
    """
    assert COMPLETE_COLUMNS.issubset(set(payout_df.columns))

    overdrafts: list[Overdraft] = []
    transfers: list[Transfer] = []
    for _, payment in payout_df.iterrows():
        payout_datum = RewardAndPenaltyDatum.from_series(payment)
        if payout_datum.is_overdraft():
            overdraft = Overdraft(
                period=period,
                account=payout_datum.solver,
                name=payout_datum.solver_name,
                wei=-int(payout_datum.total_outgoing_eth()),
            )
            print(f"Solver Overdraft! {overdraft}")
            overdrafts.append(overdraft)
        else:
            # No overdraft, always results in at least one transfer.
            transfers += payout_datum.as_payouts()

    return PeriodPayouts(overdrafts, transfers)


def validate_df_columns(
    payment_df: DataFrame, slippage_df: DataFrame, reward_target_df: DataFrame
) -> None:
    """
    Since we are working with dataframes rather than concrete objects,
    we validate that the expected columns/fields are available within our datasets.
    While it is ok for the input data to contain more columns,
    this method merely validates that the expected ones are there.
    """
    assert PAYMENT_COLUMNS.issubset(
        set(payment_df.columns)
    ), f"Payment validation failed with columns: {set(payment_df.columns)}"
    assert SLIPPAGE_COLUMNS.issubset(
        set(slippage_df.columns)
    ), f"Slippage validation Failed with columns: {set(slippage_df.columns)}"
    assert REWARD_TARGET_COLUMNS.issubset(
        set(reward_target_df.columns)
    ), f"Reward Target validation Failed with columns: {set(reward_target_df.columns)}"


def normalize_address_field(frame: DataFrame, column_name: str) -> None:
    """Lower-cases column_name field"""
    frame[column_name] = frame[column_name].str.lower()


def construct_payout_dataframe(
    payment_df: DataFrame, slippage_df: DataFrame, reward_target_df: DataFrame
) -> DataFrame:
    """
    Method responsible for joining datasets related to payouts.
    Namely, reward targets and slippage (coming from Dune)
    with reward and execution data coming from orderbook.
    """
    # TODO - After CIP-20 phased in, adapt query to return `solver` like all the others
    slippage_df = slippage_df.rename(columns={"solver_address": "solver"})
    # 1. Assert existence of required columns.
    validate_df_columns(payment_df, slippage_df, reward_target_df)

    # 2. Normalize Join Column (and Ethereum Address Field)
    join_column = "solver"
    normalize_address_field(payment_df, join_column)
    normalize_address_field(slippage_df, join_column)
    normalize_address_field(reward_target_df, join_column)

    # 3. Merge the three dataframes (joining on solver)
    merged_df = payment_df.merge(slippage_df, on=join_column, how="left").merge(
        reward_target_df, on=join_column, how="left"
    )
    return merged_df


def construct_payouts(
    dune: DuneFetcher, orderbook: MultiInstanceDBFetcher
) -> list[Transfer]:
    """Workflow of solver reward payout logic post-CIP20"""

    price_day = dune.period.end - timedelta(days=1)
    reward_token = TokenId.COW

    complete_payout_df = construct_payout_dataframe(
        # Fetch and extend auction data from orderbook.
        payment_df=extend_payment_df(
            pdf=orderbook.get_solver_rewards(dune.start_block, dune.end_block),
            # provide token conversion functions (ETH <--> COW)
            converter=TokenConversion(
                eth_to_token=lambda t: eth_in_token(reward_token, t, price_day),
                token_to_eth=lambda t: token_in_eth(reward_token, t, price_day),
            ),
        ),
        # Dune: Fetch Solver Slippage & Reward Targets
        slippage_df=pandas.DataFrame(dune.get_period_slippage()),
        reward_target_df=pandas.DataFrame(dune.get_vouches()),
    )
    # Sort by solver before breaking this data frame into Transfer objects.
    complete_payout_df = complete_payout_df.sort_values("solver")

    performance_reward = complete_payout_df["reward_cow"].sum()
    participation_reward = complete_payout_df["secondary_reward_cow"].sum()
    dune.log_saver.print(
        f"Performance Reward: {performance_reward / 10 ** 18:.4f}\n"
        f"Participation Reward: {participation_reward / 10 ** 18:.4f}\n",
        category=Category.TOTALS,
    )
    payouts = prepare_transfers(complete_payout_df, dune.period)
    for overdraft in payouts.overdrafts:
        dune.log_saver.print(str(overdraft), Category.OVERDRAFT)
    return payouts.transfers
