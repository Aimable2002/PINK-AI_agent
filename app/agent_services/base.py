"""
Agent services are predefined, persistent AI workers sold as a product
feature -- distinct from connectors (external tools the *chat* agent
reaches for) and distinct from the generic "automation framework" idea
that was explicitly rejected in favor of this: one real, self-contained
code file per agent type (telegram_signal_monitor.py being the first),
sharing only:
  - this file's constants/helpers,
  - the `user_agent_services` table for on/off + config storage,
  - the daemon process that drives event-driven ones,
  - tasks.charge_usage / get_remaining_credits for billing.

There is deliberately no ABC/interface class here forcing every future
agent into the same method shape -- a chat-triggered service, an
event-driven one (this one), and a scheduled/cron one are different
enough shapes that a shared interface would just get worked around.
What's actually shared is listed above; each agent module documents its
own entry point at the top of its file.
"""

from __future__ import annotations


class AgentServicePaused(Exception):

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)