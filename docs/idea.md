# c2c Idea

I have two laptops that are largely running claude-code sessions on various projects that span work and home.
At home, I'm usually on the Pro or Max p(5x) lan, depending on how hard I am pushing.
For the work projects, I have access to essentially unlimited anthropic budget.  Fable 5.1, Opus 5.5, no quota.

Some projects I work on both at home and at work, and I spread the PRs across sessions on both systems.  I try
to keep the really heavy lifting at work so as to not abuse my personal token quotas too much.

I find myself manually ferrying PR review requests back and forth, and lately also coordination - test
requests, etc.

The new claude-code drops have enabled a really cool session-discovery and session-communication feature
across sessions that are running on the same host.  Apparently it requires a local filesystem.

I'd like to explore enabling my claude sessions to discover one another and collaborate while running on 
different hosts.  My initial thinking:

1. A server hosted on a publically-accessible system provides a coordination channel.
2. The sessions gain a skill or plugin that guides them to connect to this server to look for peers working on the same projects.
3. The sessions use the A2A protocol for discovery & communication.
4. The sessions can then coordinate with one another for cross-session code reviews, test requests, etc.

Feasible?

(c2c = claude to claude)


