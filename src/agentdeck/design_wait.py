"""The owner-wait sentence every design verdict leads with (#4807).

Its own module, importing nothing, so the chat messages in ``work_release`` and the routes in
``design_review`` share it without ``work_release`` importing the design review.
"""

#: A design review's verdict does not answer the owner's own question (#4476, #4807): while the
#: owner's question box about a redesign answering a design finding is open, every "build to it"
#: waits on every answer in that box, and then the owner's pick decides, which may be something
#: other than the redesign; "build to it" is only for a design that is no such redesign.
OWNER_WAIT = (
    "If this design is a redesign answering a design finding, the owner's answers in your"
    " question box decide what you build, not this verdict: while any question there (the"
    " redesign or a mechanism check) is unanswered, write no code for that problem (fix other"
    " findings as usual); once all are answered, do what they picked, whether that is the"
    " redesign, a narrow fix, simplifying, rethinking it together, shipping now or a pause."
)
