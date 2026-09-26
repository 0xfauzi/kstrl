// Recorded Jev answers, exported by jev-bench/export_replay.py. Model jev-1.13.0, 2026-09-26.
// Nothing here is invented: each entry is one real request and its answer.
window.JEV_REPLAY = [
 {
  "text": "cost?",
  "route": "ask_cost",
  "conf": 0.67,
  "top": [
   [
    "ask_cost",
    0.69
   ],
   [
    "no_match",
    0.23
   ],
   [
    "make_view",
    0.03
   ],
   [
    "open_decisions",
    0.02
   ]
  ],
  "args": {
   "cost_scope": {
    "value": "unstated",
    "conf": 0.97
   },
   "run": {
    "value": "not named",
    "conf": 0.94
   },
   "period": {
    "value": "unstated",
    "conf": 1.0
   }
  },
  "latency_ms": 213,
  "tokens": 3063,
  "expected": "ask_cost",
  "correct": true
 },
 {
  "text": "why did client-commands fail",
  "route": "ask_why_failed",
  "conf": 0.99,
  "top": [
   [
    "ask_why_failed",
    0.99
   ],
   [
    "no_match",
    0.01
   ],
   [
    "ci_poll",
    0.0
   ],
   [
    "ask_alive",
    0.0
   ]
  ],
  "args": {
   "component": {
    "value": "client-commands",
    "conf": 1.0
   },
   "run": {
    "value": "8d80e8",
    "conf": 0.54
   }
  },
  "latency_ms": 236,
  "tokens": 3068,
  "expected": "ask_why_failed",
  "correct": true
 },
 {
  "text": "why did token-crypto fail",
  "route": "ask_why_failed",
  "conf": 1.0,
  "top": [
   [
    "ask_why_failed",
    1.0
   ],
   [
    "snooze",
    0.0
   ],
   [
    "open_history",
    0.0
   ],
   [
    "retry",
    0.0
   ]
  ],
  "args": {
   "component": {
    "value": "token-crypto",
    "conf": 1.0
   },
   "run": {
    "value": "not named",
    "conf": 0.63
   }
  },
  "latency_ms": 275,
  "tokens": 3068,
  "expected": "ask_why_failed",
  "correct": true
 },
 {
  "text": "is main green",
  "route": "ask_main_green",
  "conf": 0.99,
  "top": [
   [
    "ask_main_green",
    1.0
   ],
   [
    "snooze",
    0.0
   ],
   [
    "open_history",
    0.0
   ],
   [
    "ci_poll",
    0.0
   ]
  ],
  "args": {},
  "latency_ms": 234,
  "tokens": 3064,
  "expected": "ask_main_green",
  "correct": true
 },
 {
  "text": "did the merges pass ci",
  "route": "ask_main_green",
  "conf": 0.93,
  "top": [
   [
    "ask_main_green",
    0.93
   ],
   [
    "open_delivery",
    0.04
   ],
   [
    "ci_poll",
    0.03
   ],
   [
    "open_run",
    0.0
   ]
  ],
  "args": {},
  "latency_ms": 243,
  "tokens": 3066,
  "expected": "ask_main_green",
  "correct": true
 },
 {
  "text": "what needs me",
  "route": "ask_needs_me",
  "conf": 0.98,
  "top": [
   [
    "ask_needs_me",
    0.99
   ],
   [
    "open_decisions",
    0.01
   ],
   [
    "toggle_theme",
    0.0
   ],
   [
    "open_run",
    0.0
   ]
  ],
  "args": {},
  "latency_ms": 260,
  "tokens": 3064,
  "expected": "ask_needs_me",
  "correct": true
 },
 {
  "text": "anything waiting on me?",
  "route": "ask_needs_me",
  "conf": 0.95,
  "top": [
   [
    "ask_needs_me",
    0.95
   ],
   [
    "open_decisions",
    0.05
   ],
   [
    "open_delivery",
    0.0
   ],
   [
    "snooze",
    0.0
   ]
  ],
  "args": {},
  "latency_ms": 228,
  "tokens": 3066,
  "expected": "ask_needs_me",
  "correct": true
 },
 {
  "text": "is the agent alive",
  "route": "ask_alive",
  "conf": 1.0,
  "top": [
   [
    "ask_alive",
    1.0
   ],
   [
    "ask_cost",
    0.0
   ],
   [
    "open_component",
    0.0
   ],
   [
    "open_serve",
    0.0
   ]
  ],
  "args": {
   "component": {
    "value": "not named",
    "conf": 0.93
   }
  },
  "latency_ms": 237,
  "tokens": 3065,
  "expected": "ask_alive",
  "correct": true
 },
 {
  "text": "is http-app still doing anything",
  "route": "ask_alive",
  "conf": 0.98,
  "top": [
   [
    "ask_alive",
    0.99
   ],
   [
    "open_component",
    0.01
   ],
   [
    "toggle_theme",
    0.0
   ],
   [
    "open_config",
    0.0
   ]
  ],
  "args": {
   "component": {
    "value": "http-app",
    "conf": 1.0
   }
  },
  "latency_ms": 232,
  "tokens": 3067,
  "expected": "ask_alive",
  "correct": true
 },
 {
  "text": "how much have we spent today",
  "route": "ask_cost",
  "conf": 0.99,
  "top": [
   [
    "ask_cost",
    0.99
   ],
   [
    "make_view",
    0.01
   ],
   [
    "decide",
    0.0
   ],
   [
    "open_serve",
    0.0
   ]
  ],
  "args": {
   "cost_scope": {
    "value": "unstated",
    "conf": 0.58
   },
   "run": {
    "value": "not named",
    "conf": 0.69
   },
   "period": {
    "value": "today",
    "conf": 1.0
   }
  },
  "latency_ms": 251,
  "tokens": 3067,
  "expected": "ask_cost",
  "correct": true
 },
 {
  "text": "spend on live01",
  "route": "open_run",
  "conf": 0.79,
  "top": [
   [
    "open_run",
    0.8
   ],
   [
    "ask_cost",
    0.13
   ],
   [
    "make_view",
    0.04
   ],
   [
    "no_match",
    0.03
   ]
  ],
  "args": {
   "run": {
    "value": "live01",
    "conf": 1.0
   }
  },
  "latency_ms": 262,
  "tokens": 3067,
  "expected": "ask_cost",
  "correct": false
 },
 {
  "text": "total cost this week",
  "route": "ask_cost",
  "conf": 0.88,
  "top": [
   [
    "ask_cost",
    0.9
   ],
   [
    "make_view",
    0.1
   ],
   [
    "open_component",
    0.0
   ],
   [
    "open_serve",
    0.0
   ]
  ],
  "args": {
   "cost_scope": {
    "value": "all_runs",
    "conf": 0.8
   },
   "run": {
    "value": "not named",
    "conf": 0.9
   },
   "period": {
    "value": "this_week",
    "conf": 1.0
   }
  },
  "latency_ms": 262,
  "tokens": 3065,
  "expected": "ask_cost",
  "correct": true
 },
 {
  "text": "open live01",
  "route": "open_run",
  "conf": 0.96,
  "top": [
   [
    "open_run",
    0.97
   ],
   [
    "no_match",
    0.02
   ],
   [
    "open_component",
    0.01
   ],
   [
    "open_history",
    0.0
   ]
  ],
  "args": {
   "run": {
    "value": "live01",
    "conf": 1.0
   }
  },
  "latency_ms": 238,
  "tokens": 3065,
  "expected": "open_run",
  "correct": true
 },
 {
  "text": "8d80e8",
  "route": "open_run",
  "conf": 0.94,
  "top": [
   [
    "open_run",
    0.95
   ],
   [
    "open_decisions",
    0.02
   ],
   [
    "open_delivery",
    0.01
   ],
   [
    "ask_needs_me",
    0.01
   ]
  ],
  "args": {
   "run": {
    "value": "8d80e8",
    "conf": 1.0
   }
  },
  "latency_ms": 220,
  "tokens": 3067,
  "expected": "open_run",
  "correct": true
 },
 {
  "text": "show me the live run",
  "route": "open_run",
  "conf": 0.98,
  "top": [
   [
    "open_run",
    0.97
   ],
   [
    "ask_alive",
    0.01
   ],
   [
    "open_decisions",
    0.01
   ],
   [
    "no_match",
    0.01
   ]
  ],
  "args": {
   "run": {
    "value": "the live run",
    "conf": 0.69
   }
  },
  "latency_ms": 263,
  "tokens": 3066,
  "expected": "open_run",
  "correct": true
 },
 {
  "text": "http-app",
  "route": "open_component",
  "conf": 0.75,
  "top": [
   [
    "open_component",
    0.77
   ],
   [
    "no_match",
    0.08
   ],
   [
    "open_decisions",
    0.06
   ],
   [
    "start_factory",
    0.02
   ]
  ],
  "args": {
   "component": {
    "value": "http-app",
    "conf": 1.0
   }
  },
  "latency_ms": 241,
  "tokens": 3063,
  "expected": "open_component",
  "correct": true
 },
 {
  "text": "go to storage",
  "route": "open_component",
  "conf": 0.96,
  "top": [
   [
    "open_component",
    0.98
   ],
   [
    "no_match",
    0.02
   ],
   [
    "start_factory",
    0.0
   ],
   [
    "serve_control",
    0.0
   ]
  ],
  "args": {
   "component": {
    "value": "storage",
    "conf": 1.0
   }
  },
  "latency_ms": 258,
  "tokens": 3064,
  "expected": "open_component",
  "correct": true
 },
 {
  "text": "decisions",
  "route": "open_decisions",
  "conf": 0.91,
  "top": [
   [
    "open_decisions",
    0.92
   ],
   [
    "decide",
    0.05
   ],
   [
    "ask_needs_me",
    0.02
   ],
   [
    "no_match",
    0.01
   ]
  ],
  "args": {},
  "latency_ms": 226,
  "tokens": 3064,
  "expected": "open_decisions",
  "correct": true
 },
 {
  "text": "inbox",
  "route": "open_decisions",
  "conf": 0.43,
  "top": [
   [
    "open_decisions",
    0.46
   ],
   [
    "no_match",
    0.3
   ],
   [
    "ask_needs_me",
    0.16
   ],
   [
    "make_view",
    0.04
   ]
  ],
  "args": {},
  "latency_ms": 234,
  "tokens": 3063,
  "expected": "open_decisions",
  "correct": true
 },
 {
  "text": "failures",
  "route": "open_failures",
  "conf": 0.98,
  "top": [
   [
    "open_failures",
    0.99
   ],
   [
    "no_match",
    0.01
   ],
   [
    "start_factory",
    0.0
   ],
   [
    "ask_why_failed",
    0.0
   ]
  ],
  "args": {},
  "latency_ms": 228,
  "tokens": 3064,
  "expected": "open_failures",
  "correct": true
 },
 {
  "text": "what failed",
  "route": "ask_why_failed",
  "conf": 0.75,
  "top": [
   [
    "ask_why_failed",
    0.77
   ],
   [
    "open_failures",
    0.2
   ],
   [
    "no_match",
    0.03
   ],
   [
    "snooze",
    0.0
   ]
  ],
  "args": {
   "component": {
    "value": "not named",
    "conf": 0.95
   },
   "run": {
    "value": "not named",
    "conf": 0.65
   }
  },
  "latency_ms": 256,
  "tokens": 3063,
  "expected": "open_failures",
  "correct": true
 },
 {
  "text": "delivery",
  "route": "open_delivery",
  "conf": 0.98,
  "top": [
   [
    "open_delivery",
    0.98
   ],
   [
    "no_match",
    0.01
   ],
   [
    "open_decisions",
    0.01
   ],
   [
    "start_factory",
    0.0
   ]
  ],
  "args": {},
  "latency_ms": 229,
  "tokens": 3062,
  "expected": "open_delivery",
  "correct": true
 },
 {
  "text": "ci",
  "route": "ci_poll",
  "conf": 0.47,
  "top": [
   [
    "ci_poll",
    0.5
   ],
   [
    "open_delivery",
    0.19
   ],
   [
    "open_decisions",
    0.13
   ],
   [
    "ask_main_green",
    0.07
   ]
  ],
  "args": {},
  "latency_ms": 217,
  "tokens": 3062,
  "expected": "open_delivery",
  "correct": true
 },
 {
  "text": "serve queue",
  "route": "open_serve",
  "conf": 0.86,
  "top": [
   [
    "open_serve",
    0.87
   ],
   [
    "serve_control",
    0.05
   ],
   [
    "open_decisions",
    0.04
   ],
   [
    "no_match",
    0.02
   ]
  ],
  "args": {},
  "latency_ms": 254,
  "tokens": 3063,
  "expected": "open_serve",
  "correct": true
 },
 {
  "text": "whats queued",
  "route": "open_decisions",
  "conf": 0.62,
  "top": [
   [
    "open_decisions",
    0.65
   ],
   [
    "ask_needs_me",
    0.28
   ],
   [
    "open_serve",
    0.05
   ],
   [
    "no_match",
    0.01
   ]
  ],
  "args": {},
  "latency_ms": 233,
  "tokens": 3064,
  "expected": "open_serve",
  "correct": false
 },
 {
  "text": "config",
  "route": "open_config",
  "conf": 0.93,
  "top": [
   [
    "open_config",
    0.94
   ],
   [
    "ask_needs_me",
    0.02
   ],
   [
    "no_match",
    0.02
   ],
   [
    "make_view",
    0.01
   ]
  ],
  "args": {},
  "latency_ms": 207,
  "tokens": 3062,
  "expected": "open_config",
  "correct": true
 },
 {
  "text": "what is the cost cap",
  "route": "ask_cost",
  "conf": 0.96,
  "top": [
   [
    "ask_cost",
    0.97
   ],
   [
    "no_match",
    0.03
   ],
   [
    "ask_alive",
    0.0
   ],
   [
    "retry",
    0.0
   ]
  ],
  "args": {
   "cost_scope": {
    "value": "unstated",
    "conf": 0.99
   },
   "run": {
    "value": "not named",
    "conf": 0.98
   },
   "period": {
    "value": "unstated",
    "conf": 1.0
   }
  },
  "latency_ms": 314,
  "tokens": 3066,
  "expected": "open_config",
  "correct": true
 },
 {
  "text": "learning",
  "route": "open_learning",
  "conf": 0.94,
  "top": [
   [
    "open_learning",
    0.95
   ],
   [
    "no_match",
    0.04
   ],
   [
    "open_decisions",
    0.01
   ],
   [
    "ask_main_green",
    0.0
   ]
  ],
  "args": {},
  "latency_ms": 252,
  "tokens": 3062,
  "expected": "open_learning",
  "correct": true
 },
 {
  "text": "recurring failure patterns",
  "route": "open_learning",
  "conf": 0.96,
  "top": [
   [
    "open_learning",
    0.97
   ],
   [
    "no_match",
    0.02
   ],
   [
    "open_failures",
    0.01
   ],
   [
    "open_config",
    0.0
   ]
  ],
  "args": {},
  "latency_ms": 206,
  "tokens": 3066,
  "expected": "open_learning",
  "correct": true
 },
 {
  "text": "history",
  "route": "open_history",
  "conf": 0.82,
  "top": [
   [
    "open_history",
    0.84
   ],
   [
    "no_match",
    0.12
   ],
   [
    "open_decisions",
    0.02
   ],
   [
    "make_view",
    0.01
   ]
  ],
  "args": {},
  "latency_ms": 239,
  "tokens": 3062,
  "expected": "open_history",
  "correct": true
 },
 {
  "text": "past runs",
  "route": "open_history",
  "conf": 0.99,
  "top": [
   [
    "open_history",
    1.0
   ],
   [
    "start_factory",
    0.0
   ],
   [
    "ci_poll",
    0.0
   ],
   [
    "make_view",
    0.0
   ]
  ],
  "args": {},
  "latency_ms": 236,
  "tokens": 3063,
  "expected": "open_history",
  "correct": true
 },
 {
  "text": "approve client-commands",
  "route": "decide",
  "conf": 0.98,
  "top": [
   [
    "decide",
    0.99
   ],
   [
    "no_match",
    0.01
   ],
   [
    "open_run",
    0.0
   ],
   [
    "open_component",
    0.0
   ]
  ],
  "args": {
   "decide_choice": {
    "value": "approve_run",
    "conf": 0.45
   },
   "target_item": {
    "value": "merge gate: client-commands (run 8d80e8)",
    "conf": 0.97
   }
  },
  "latency_ms": 274,
  "tokens": 3066,
  "expected": "decide",
  "correct": true
 },
 {
  "text": "approve and merge comp-c",
  "route": "decide",
  "conf": 1.0,
  "top": [
   [
    "decide",
    1.0
   ],
   [
    "snooze",
    0.0
   ],
   [
    "ask_cost",
    0.0
   ],
   [
    "retry",
    0.0
   ]
  ],
  "args": {
   "decide_choice": {
    "value": "approve_run",
    "conf": 1.0
   },
   "target_item": {
    "value": "checkpoint: comp-c (approve PR creation and merge)",
    "conf": 1.0
   }
  },
  "latency_ms": 234,
  "tokens": 3066,
  "expected": "decide",
  "correct": true
 },
 {
  "text": "approve only, don't run anything",
  "route": "decide",
  "conf": 0.97,
  "top": [
   [
    "decide",
    0.98
   ],
   [
    "no_match",
    0.01
   ],
   [
    "open_decisions",
    0.01
   ],
   [
    "open_config",
    0.0
   ]
  ],
  "args": {
   "decide_choice": {
    "value": "approve_only",
    "conf": 1.0
   },
   "target_item": {
    "value": "checkpoint: comp-c (approve PR creation and merge)",
    "conf": 0.92
   }
  },
  "latency_ms": 212,
  "tokens": 3068,
  "expected": "decide",
  "correct": true
 },
 {
  "text": "reject comp-c",
  "route": "decide",
  "conf": 1.0,
  "top": [
   [
    "decide",
    1.0
   ],
   [
    "open_history",
    0.0
   ],
   [
    "serve_control",
    0.0
   ],
   [
    "retry",
    0.0
   ]
  ],
  "args": {
   "decide_choice": {
    "value": "reject",
    "conf": 1.0
   },
   "target_item": {
    "value": "checkpoint: comp-c (approve PR creation and merge)",
    "conf": 1.0
   }
  },
  "latency_ms": 227,
  "tokens": 3064,
  "expected": "decide",
  "correct": true
 },
 {
  "text": "send comp-c back to the engineer",
  "route": "decide",
  "conf": 0.99,
  "top": [
   [
    "decide",
    1.0
   ],
   [
    "open_learning",
    0.0
   ],
   [
    "no_match",
    0.0
   ],
   [
    "ask_main_green",
    0.0
   ]
  ],
  "args": {
   "decide_choice": {
    "value": "send_back",
    "conf": 1.0
   },
   "target_item": {
    "value": "checkpoint: comp-c (approve PR creation and merge)",
    "conf": 0.97
   }
  },
  "latency_ms": 252,
  "tokens": 3068,
  "expected": "decide",
  "correct": true
 },
 {
  "text": "decide later",
  "route": "snooze",
  "conf": 0.74,
  "top": [
   [
    "snooze",
    0.76
   ],
   [
    "decide",
    0.17
   ],
   [
    "open_decisions",
    0.03
   ],
   [
    "no_match",
    0.03
   ]
  ],
  "args": {
   "target_item": {
    "value": "not named",
    "conf": 0.93
   },
   "snooze_duration": {
    "value": "unstated",
    "conf": 1.0
   }
  },
  "latency_ms": 339,
  "tokens": 3064,
  "expected": "decide",
  "correct": false
 },
 {
  "text": "retry client-commands",
  "route": "retry",
  "conf": 0.98,
  "top": [
   [
    "retry",
    0.99
   ],
   [
    "open_failures",
    0.01
   ],
   [
    "ask_why_failed",
    0.0
   ],
   [
    "no_match",
    0.0
   ]
  ],
  "args": {
   "component": {
    "value": "client-commands",
    "conf": 1.0
   }
  },
  "latency_ms": 300,
  "tokens": 3066,
  "expected": "retry",
  "correct": true
 },
 {
  "text": "rerun token-crypto",
  "route": "retry",
  "conf": 0.96,
  "top": [
   [
    "retry",
    0.96
   ],
   [
    "open_component",
    0.02
   ],
   [
    "start_factory",
    0.01
   ],
   [
    "no_match",
    0.01
   ]
  ],
  "args": {
   "component": {
    "value": "token-crypto",
    "conf": 1.0
   }
  },
  "latency_ms": 265,
  "tokens": 3067,
  "expected": "retry",
  "correct": true
 },
 {
  "text": "retry",
  "route": "retry",
  "conf": 0.96,
  "top": [
   [
    "retry",
    0.96
   ],
   [
    "open_failures",
    0.02
   ],
   [
    "no_match",
    0.01
   ],
   [
    "open_decisions",
    0.01
   ]
  ],
  "args": {
   "component": {
    "value": "not named",
    "conf": 0.88
   }
  },
  "latency_ms": 231,
  "tokens": 3062,
  "expected": "retry",
  "correct": true
 },
 {
  "text": "snooze the merge gate for a day",
  "route": "snooze",
  "conf": 0.99,
  "top": [
   [
    "snooze",
    0.99
   ],
   [
    "decide",
    0.01
   ],
   [
    "open_history",
    0.0
   ],
   [
    "stop_run",
    0.0
   ]
  ],
  "args": {
   "target_item": {
    "value": "merge gate: client-commands (run 8d80e8)",
    "conf": 1.0
   },
   "snooze_duration": {
    "value": "24h",
    "conf": 1.0
   }
  },
  "latency_ms": 236,
  "tokens": 3070,
  "expected": "snooze",
  "correct": true
 },
 {
  "text": "hide the halted run for a week",
  "route": "snooze",
  "conf": 1.0,
  "top": [
   [
    "snooze",
    1.0
   ],
   [
    "open_history",
    0.0
   ],
   [
    "open_run",
    0.0
   ],
   [
    "open_learning",
    0.0
   ]
  ],
  "args": {
   "target_item": {
    "value": "halted run fda682 (the integration loop stopped)",
    "conf": 1.0
   },
   "snooze_duration": {
    "value": "7d",
    "conf": 1.0
   }
  },
  "latency_ms": 231,
  "tokens": 3068,
  "expected": "snooze",
  "correct": true
 },
 {
  "text": "start a factory run from spec.md",
  "route": "start_factory",
  "conf": 1.0,
  "top": [
   [
    "start_factory",
    1.0
   ],
   [
    "open_config",
    0.0
   ],
   [
    "ask_alive",
    0.0
   ],
   [
    "open_learning",
    0.0
   ]
  ],
  "args": {},
  "latency_ms": 224,
  "tokens": 3068,
  "expected": "start_factory",
  "correct": true
 },
 {
  "text": "run the factory on slice 4",
  "route": "start_factory",
  "conf": 0.56,
  "top": [
   [
    "start_factory",
    0.59
   ],
   [
    "open_run",
    0.29
   ],
   [
    "no_match",
    0.07
   ],
   [
    "retry",
    0.02
   ]
  ],
  "args": {},
  "latency_ms": 247,
  "tokens": 3068,
  "expected": "start_factory",
  "correct": true
 },
 {
  "text": "stop live01",
  "route": "stop_run",
  "conf": 1.0,
  "top": [
   [
    "stop_run",
    1.0
   ],
   [
    "snooze",
    0.0
   ],
   [
    "open_history",
    0.0
   ],
   [
    "ci_poll",
    0.0
   ]
  ],
  "args": {
   "run": {
    "value": "live01",
    "conf": 1.0
   }
  },
  "latency_ms": 240,
  "tokens": 3065,
  "expected": "stop_run",
  "correct": true
 },
 {
  "text": "kill the run",
  "route": "stop_run",
  "conf": 0.99,
  "top": [
   [
    "stop_run",
    1.0
   ],
   [
    "snooze",
    0.0
   ],
   [
    "open_delivery",
    0.0
   ],
   [
    "toggle_theme",
    0.0
   ]
  ],
  "args": {
   "run": {
    "value": "the live run",
    "conf": 0.44
   }
  },
  "latency_ms": 219,
  "tokens": 3064,
  "expected": "stop_run",
  "correct": true
 },
 {
  "text": "pause serve",
  "route": "serve_control",
  "conf": 1.0,
  "top": [
   [
    "serve_control",
    1.0
   ],
   [
    "make_view",
    0.0
   ],
   [
    "open_learning",
    0.0
   ],
   [
    "open_config",
    0.0
   ]
  ],
  "args": {
   "serve_action": {
    "value": "pause",
    "conf": 1.0
   }
  },
  "latency_ms": 260,
  "tokens": 3063,
  "expected": "serve_control",
  "correct": true
 },
 {
  "text": "resume the queue",
  "route": "serve_control",
  "conf": 0.98,
  "top": [
   [
    "serve_control",
    0.99
   ],
   [
    "open_serve",
    0.01
   ],
   [
    "snooze",
    0.0
   ],
   [
    "ask_cost",
    0.0
   ]
  ],
  "args": {
   "serve_action": {
    "value": "resume",
    "conf": 1.0
   }
  },
  "latency_ms": 209,
  "tokens": 3064,
  "expected": "serve_control",
  "correct": true
 },
 {
  "text": "poll ci now",
  "route": "ci_poll",
  "conf": 0.99,
  "top": [
   [
    "ci_poll",
    1.0
   ],
   [
    "open_run",
    0.0
   ],
   [
    "ask_cost",
    0.0
   ],
   [
    "open_component",
    0.0
   ]
  ],
  "args": {},
  "latency_ms": 281,
  "tokens": 3064,
  "expected": "ci_poll",
  "correct": true
 },
 {
  "text": "refresh the ci state",
  "route": "ci_poll",
  "conf": 0.93,
  "top": [
   [
    "ci_poll",
    0.95
   ],
   [
    "open_delivery",
    0.02
   ],
   [
    "no_match",
    0.01
   ],
   [
    "retry",
    0.01
   ]
  ],
  "args": {},
  "latency_ms": 236,
  "tokens": 3065,
  "expected": "ci_poll",
  "correct": true
 },
 {
  "text": "dark mode",
  "route": "toggle_theme",
  "conf": 0.98,
  "top": [
   [
    "toggle_theme",
    0.99
   ],
   [
    "no_match",
    0.01
   ],
   [
    "open_run",
    0.0
   ],
   [
    "ask_cost",
    0.0
   ]
  ],
  "args": {
   "theme": {
    "value": "dark",
    "conf": 1.0
   }
  },
  "latency_ms": 262,
  "tokens": 3063,
  "expected": "toggle_theme",
  "correct": true
 },
 {
  "text": "switch to light",
  "route": "toggle_theme",
  "conf": 1.0,
  "top": [
   [
    "toggle_theme",
    1.0
   ],
   [
    "ask_main_green",
    0.0
   ],
   [
    "ci_poll",
    0.0
   ],
   [
    "ask_needs_me",
    0.0
   ]
  ],
  "args": {
   "theme": {
    "value": "light",
    "conf": 1.0
   }
  },
  "latency_ms": 226,
  "tokens": 3064,
  "expected": "toggle_theme",
  "correct": true
 },
 {
  "text": "show me failed components this week by cost as a timeline",
  "route": "make_view",
  "conf": 1.0,
  "top": [
   [
    "make_view",
    1.0
   ],
   [
    "toggle_theme",
    0.0
   ],
   [
    "ask_main_green",
    0.0
   ],
   [
    "ask_cost",
    0.0
   ]
  ],
  "args": {
   "view_source": {
    "value": "components",
    "conf": 0.53
   },
   "view_form": {
    "value": "timeline",
    "conf": 1.0
   },
   "view_group": {
    "value": "component",
    "conf": 0.45
   },
   "view_sort": {
    "value": "cost",
    "conf": 0.79
   },
   "view_filter_state": {
    "value": "failed",
    "conf": 1.0
   },
   "period": {
    "value": "this_week",
    "conf": 1.0
   },
   "view_placement": {
    "value": "unstated",
    "conf": 0.96
   },
   "component": {
    "value": "not named",
    "conf": 0.93
   },
   "run": {
    "value": "not named",
    "conf": 0.81
   }
  },
  "latency_ms": 252,
  "tokens": 3072,
  "expected": "make_view",
  "correct": true
 },
 {
  "text": "cost per run as a sparkline",
  "route": "make_view",
  "conf": 1.0,
  "top": [
   [
    "make_view",
    1.0
   ],
   [
    "open_run",
    0.0
   ],
   [
    "toggle_theme",
    0.0
   ],
   [
    "stop_run",
    0.0
   ]
  ],
  "args": {
   "view_source": {
    "value": "costs",
    "conf": 0.99
   },
   "view_form": {
    "value": "sparkline",
    "conf": 1.0
   },
   "view_group": {
    "value": "run",
    "conf": 0.78
   },
   "view_sort": {
    "value": "cost",
    "conf": 0.51
   },
   "view_filter_state": {
    "value": "all",
    "conf": 0.64
   },
   "period": {
    "value": "unstated",
    "conf": 0.85
   },
   "view_placement": {
    "value": "unstated",
    "conf": 0.87
   },
   "component": {
    "value": "not named",
    "conf": 0.93
   },
   "run": {
    "value": "not named",
    "conf": 0.78
   }
  },
  "latency_ms": 235,
  "tokens": 3068,
  "expected": "make_view",
  "correct": true
 },
 {
  "text": "board of components by state",
  "route": "make_view",
  "conf": 1.0,
  "top": [
   [
    "make_view",
    1.0
   ],
   [
    "open_history",
    0.0
   ],
   [
    "serve_control",
    0.0
   ],
   [
    "start_factory",
    0.0
   ]
  ],
  "args": {
   "view_source": {
    "value": "components",
    "conf": 0.98
   },
   "view_form": {
    "value": "board",
    "conf": 0.99
   },
   "view_group": {
    "value": "state",
    "conf": 0.93
   },
   "view_sort": {
    "value": "state",
    "conf": 0.99
   },
   "view_filter_state": {
    "value": "all",
    "conf": 0.84
   },
   "period": {
    "value": "unstated",
    "conf": 1.0
   },
   "view_placement": {
    "value": "unstated",
    "conf": 0.96
   },
   "component": {
    "value": "not named",
    "conf": 0.81
   },
   "run": {
    "value": "not named",
    "conf": 0.94
   }
  },
  "latency_ms": 220,
  "tokens": 3066,
  "expected": "make_view",
  "correct": true
 },
 {
  "text": "table of findings by severity",
  "route": "make_view",
  "conf": 1.0,
  "top": [
   [
    "make_view",
    1.0
   ],
   [
    "serve_control",
    0.0
   ],
   [
    "open_run",
    0.0
   ],
   [
    "open_component",
    0.0
   ]
  ],
  "args": {
   "view_source": {
    "value": "findings",
    "conf": 1.0
   },
   "view_form": {
    "value": "table",
    "conf": 1.0
   },
   "view_group": {
    "value": "severity",
    "conf": 1.0
   },
   "view_sort": {
    "value": "state",
    "conf": 0.46
   },
   "view_filter_state": {
    "value": "all",
    "conf": 0.9
   },
   "period": {
    "value": "unstated",
    "conf": 1.0
   },
   "view_placement": {
    "value": "unstated",
    "conf": 0.98
   },
   "component": {
    "value": "not named",
    "conf": 0.98
   },
   "run": {
    "value": "not named",
    "conf": 0.98
   }
  },
  "latency_ms": 268,
  "tokens": 3066,
  "expected": "make_view",
  "correct": true
 },
 {
  "text": "timeline of live01's phases",
  "route": "make_view",
  "conf": 0.75,
  "top": [
   [
    "make_view",
    0.77
   ],
   [
    "open_run",
    0.23
   ],
   [
    "open_history",
    0.0
   ],
   [
    "stop_run",
    0.0
   ]
  ],
  "args": {
   "view_source": {
    "value": "events",
    "conf": 0.97
   },
   "view_form": {
    "value": "timeline",
    "conf": 1.0
   },
   "view_group": {
    "value": "phase",
    "conf": 0.89
   },
   "view_sort": {
    "value": "time",
    "conf": 0.98
   },
   "view_filter_state": {
    "value": "all",
    "conf": 0.75
   },
   "period": {
    "value": "unstated",
    "conf": 0.59
   },
   "view_placement": {
    "value": "unstated",
    "conf": 0.95
   },
   "component": {
    "value": "not named",
    "conf": 0.93
   },
   "run": {
    "value": "live01",
    "conf": 0.98
   }
  },
  "latency_ms": 233,
  "tokens": 3068,
  "expected": "make_view",
  "correct": true
 },
 {
  "text": "tokens over the last month",
  "route": "make_view",
  "conf": 0.79,
  "top": [
   [
    "make_view",
    0.8
   ],
   [
    "ask_cost",
    0.11
   ],
   [
    "no_match",
    0.07
   ],
   [
    "open_history",
    0.01
   ]
  ],
  "args": {
   "view_source": {
    "value": "costs",
    "conf": 0.99
   },
   "view_form": {
    "value": "sparkline",
    "conf": 0.55
   },
   "view_group": {
    "value": "day",
    "conf": 0.76
   },
   "view_sort": {
    "value": "time",
    "conf": 0.87
   },
   "view_filter_state": {
    "value": "all",
    "conf": 0.81
   },
   "period": {
    "value": "this_month",
    "conf": 1.0
   },
   "view_placement": {
    "value": "unstated",
    "conf": 0.94
   },
   "component": {
    "value": "token-crypto",
    "conf": 0.63
   },
   "run": {
    "value": "not named",
    "conf": 0.9
   }
  },
  "latency_ms": 275,
  "tokens": 3066,
  "expected": "make_view",
  "correct": true
 },
 {
  "text": "a meter of spend against the cap, dock it",
  "route": "make_view",
  "conf": 0.25,
  "top": [
   [
    "make_view",
    0.29
   ],
   [
    "ask_cost",
    0.29
   ],
   [
    "decide",
    0.15
   ],
   [
    "no_match",
    0.13
   ]
  ],
  "args": {
   "view_source": {
    "value": "costs",
    "conf": 0.96
   },
   "view_form": {
    "value": "meter",
    "conf": 0.99
   },
   "view_group": {
    "value": "state",
    "conf": 0.32
   },
   "view_sort": {
    "value": "cost",
    "conf": 0.73
   },
   "view_filter_state": {
    "value": "all",
    "conf": 0.25
   },
   "period": {
    "value": "unstated",
    "conf": 0.97
   },
   "view_placement": {
    "value": "dock",
    "conf": 0.97
   },
   "component": {
    "value": "not named",
    "conf": 0.7
   },
   "run": {
    "value": "not named",
    "conf": 0.72
   }
  },
  "latency_ms": 268,
  "tokens": 3071,
  "expected": "make_view",
  "correct": true
 },
 {
  "text": "list runs sorted by cost",
  "route": "make_view",
  "conf": 0.99,
  "top": [
   [
    "make_view",
    1.0
   ],
   [
    "ask_alive",
    0.0
   ],
   [
    "open_serve",
    0.0
   ],
   [
    "open_component",
    0.0
   ]
  ],
  "args": {
   "view_source": {
    "value": "costs",
    "conf": 0.54
   },
   "view_form": {
    "value": "list",
    "conf": 0.97
   },
   "view_group": {
    "value": "none",
    "conf": 0.86
   },
   "view_sort": {
    "value": "cost",
    "conf": 1.0
   },
   "view_filter_state": {
    "value": "all",
    "conf": 0.94
   },
   "period": {
    "value": "unstated",
    "conf": 1.0
   },
   "view_placement": {
    "value": "unstated",
    "conf": 0.92
   },
   "component": {
    "value": "not named",
    "conf": 0.86
   },
   "run": {
    "value": "not named",
    "conf": 0.96
   }
  },
  "latency_ms": 239,
  "tokens": 3066,
  "expected": "make_view",
  "correct": true
 },
 {
  "text": "graph of the components and their dependencies",
  "route": "make_view",
  "conf": 1.0,
  "top": [
   [
    "make_view",
    1.0
   ],
   [
    "stop_run",
    0.0
   ],
   [
    "ask_cost",
    0.0
   ],
   [
    "open_history",
    0.0
   ]
  ],
  "args": {
   "view_source": {
    "value": "components",
    "conf": 0.91
   },
   "view_form": {
    "value": "graph",
    "conf": 1.0
   },
   "view_group": {
    "value": "component",
    "conf": 0.99
   },
   "view_sort": {
    "value": "none",
    "conf": 0.95
   },
   "view_filter_state": {
    "value": "all",
    "conf": 0.95
   },
   "period": {
    "value": "unstated",
    "conf": 1.0
   },
   "view_placement": {
    "value": "unstated",
    "conf": 0.96
   },
   "component": {
    "value": "not named",
    "conf": 0.92
   },
   "run": {
    "value": "not named",
    "conf": 0.97
   }
  },
  "latency_ms": 258,
  "tokens": 3068,
  "expected": "make_view",
  "correct": true
 },
 {
  "text": "how many runs failed this month, just the number",
  "route": "make_view",
  "conf": 0.89,
  "top": [
   [
    "make_view",
    0.91
   ],
   [
    "open_failures",
    0.06
   ],
   [
    "ask_cost",
    0.02
   ],
   [
    "no_match",
    0.01
   ]
  ],
  "args": {
   "view_source": {
    "value": "runs",
    "conf": 0.9
   },
   "view_form": {
    "value": "number",
    "conf": 1.0
   },
   "view_group": {
    "value": "state",
    "conf": 0.42
   },
   "view_sort": {
    "value": "none",
    "conf": 0.83
   },
   "view_filter_state": {
    "value": "failed",
    "conf": 0.99
   },
   "period": {
    "value": "this_month",
    "conf": 1.0
   },
   "view_placement": {
    "value": "unstated",
    "conf": 0.97
   },
   "component": {
    "value": "not named",
    "conf": 0.98
   },
   "run": {
    "value": "not named",
    "conf": 0.82
   }
  },
  "latency_ms": 224,
  "tokens": 3071,
  "expected": "make_view",
  "correct": true
 },
 {
  "text": "diff for comp-c",
  "route": "make_view",
  "conf": 0.67,
  "top": [
   [
    "make_view",
    0.7
   ],
   [
    "open_component",
    0.28
   ],
   [
    "no_match",
    0.01
   ],
   [
    "ci_poll",
    0.01
   ]
  ],
  "args": {
   "view_source": {
    "value": "diff",
    "conf": 0.99
   },
   "view_form": {
    "value": "diff",
    "conf": 0.99
   },
   "view_group": {
    "value": "component",
    "conf": 0.79
   },
   "view_sort": {
    "value": "none",
    "conf": 0.77
   },
   "view_filter_state": {
    "value": "waiting",
    "conf": 0.47
   },
   "period": {
    "value": "unstated",
    "conf": 1.0
   },
   "view_placement": {
    "value": "unstated",
    "conf": 0.99
   },
   "component": {
    "value": "comp-c",
    "conf": 1.0
   },
   "run": {
    "value": "not named",
    "conf": 0.89
   }
  },
  "latency_ms": 252,
  "tokens": 3065,
  "expected": "make_view",
  "correct": true
 },
 {
  "text": "findings by phase, floating",
  "route": "make_view",
  "conf": 0.98,
  "top": [
   [
    "make_view",
    0.99
   ],
   [
    "open_decisions",
    0.01
   ],
   [
    "start_factory",
    0.0
   ],
   [
    "ci_poll",
    0.0
   ]
  ],
  "args": {
   "view_source": {
    "value": "findings",
    "conf": 0.8
   },
   "view_form": {
    "value": "board",
    "conf": 0.59
   },
   "view_group": {
    "value": "phase",
    "conf": 0.97
   },
   "view_sort": {
    "value": "state",
    "conf": 0.54
   },
   "view_filter_state": {
    "value": "all",
    "conf": 0.32
   },
   "period": {
    "value": "unstated",
    "conf": 0.98
   },
   "view_placement": {
    "value": "float",
    "conf": 0.99
   },
   "component": {
    "value": "not named",
    "conf": 0.84
   },
   "run": {
    "value": "not named",
    "conf": 0.51
   }
  },
  "latency_ms": 230,
  "tokens": 3067,
  "expected": "make_view",
  "correct": true
 },
 {
  "text": "ci state per merge as a list",
  "route": "open_delivery",
  "conf": 0.47,
  "top": [
   [
    "open_delivery",
    0.51
   ],
   [
    "make_view",
    0.44
   ],
   [
    "ci_poll",
    0.03
   ],
   [
    "open_decisions",
    0.01
   ]
  ],
  "args": {},
  "latency_ms": 209,
  "tokens": 3068,
  "expected": "make_view",
  "correct": true
 },
 {
  "text": "queue as a board",
  "route": "make_view",
  "conf": 0.9,
  "top": [
   [
    "make_view",
    0.91
   ],
   [
    "open_decisions",
    0.05
   ],
   [
    "open_serve",
    0.03
   ],
   [
    "open_run",
    0.01
   ]
  ],
  "args": {
   "view_source": {
    "value": "queue",
    "conf": 0.95
   },
   "view_form": {
    "value": "board",
    "conf": 1.0
   },
   "view_group": {
    "value": "component",
    "conf": 0.2
   },
   "view_sort": {
    "value": "state",
    "conf": 0.59
   },
   "view_filter_state": {
    "value": "waiting",
    "conf": 0.52
   },
   "period": {
    "value": "unstated",
    "conf": 1.0
   },
   "view_placement": {
    "value": "dock",
    "conf": 0.28
   },
   "component": {
    "value": "not named",
    "conf": 0.78
   },
   "run": {
    "value": "not named",
    "conf": 0.73
   }
  },
  "latency_ms": 207,
  "tokens": 3065,
  "expected": "make_view",
  "correct": true
 },
 {
  "text": "running components only, as a list, newest first",
  "route": "make_view",
  "conf": 0.99,
  "top": [
   [
    "make_view",
    1.0
   ],
   [
    "open_run",
    0.0
   ],
   [
    "toggle_theme",
    0.0
   ],
   [
    "stop_run",
    0.0
   ]
  ],
  "args": {
   "view_source": {
    "value": "components",
    "conf": 0.94
   },
   "view_form": {
    "value": "list",
    "conf": 1.0
   },
   "view_group": {
    "value": "component",
    "conf": 0.52
   },
   "view_sort": {
    "value": "time",
    "conf": 1.0
   },
   "view_filter_state": {
    "value": "running",
    "conf": 0.99
   },
   "period": {
    "value": "unstated",
    "conf": 0.95
   },
   "view_placement": {
    "value": "unstated",
    "conf": 0.92
   },
   "component": {
    "value": "not named",
    "conf": 0.74
   },
   "run": {
    "value": "the live run",
    "conf": 0.38
   }
  },
  "latency_ms": 228,
  "tokens": 3071,
  "expected": "make_view",
  "correct": true
 },
 {
  "text": "what's the weather",
  "route": "no_match",
  "conf": 0.98,
  "top": [
   [
    "no_match",
    0.99
   ],
   [
    "open_decisions",
    0.01
   ],
   [
    "toggle_theme",
    0.0
   ],
   [
    "open_config",
    0.0
   ]
  ],
  "args": {},
  "latency_ms": 232,
  "tokens": 3065,
  "expected": "no_match",
  "correct": true
 },
 {
  "text": "hello",
  "route": "no_match",
  "conf": 0.65,
  "top": [
   [
    "no_match",
    0.67
   ],
   [
    "ask_alive",
    0.26
   ],
   [
    "open_decisions",
    0.04
   ],
   [
    "ask_needs_me",
    0.02
   ]
  ],
  "args": {},
  "latency_ms": 249,
  "tokens": 3062,
  "expected": "no_match",
  "correct": true
 },
 {
  "text": "delete everything",
  "route": "no_match",
  "conf": 0.94,
  "top": [
   [
    "no_match",
    0.96
   ],
   [
    "open_decisions",
    0.03
   ],
   [
    "stop_run",
    0.01
   ],
   [
    "ask_cost",
    0.0
   ]
  ],
  "args": {},
  "latency_ms": 244,
  "tokens": 3063,
  "expected": "no_match",
  "correct": true
 },
 {
  "text": "write me a poem about merges",
  "route": "no_match",
  "conf": 0.98,
  "top": [
   [
    "no_match",
    0.99
   ],
   [
    "start_factory",
    0.01
   ],
   [
    "toggle_theme",
    0.0
   ],
   [
    "ask_main_green",
    0.0
   ]
  ],
  "args": {},
  "latency_ms": 242,
  "tokens": 3067,
  "expected": "no_match",
  "correct": true
 },
 {
  "text": "git push origin main",
  "route": "no_match",
  "conf": 0.64,
  "top": [
   [
    "no_match",
    0.66
   ],
   [
    "open_delivery",
    0.22
   ],
   [
    "ci_poll",
    0.06
   ],
   [
    "ask_main_green",
    0.02
   ]
  ],
  "args": {},
  "latency_ms": 248,
  "tokens": 3065,
  "expected": "no_match",
  "correct": true
 },
 {
  "text": "asdfgh",
  "route": "no_match",
  "conf": 0.85,
  "top": [
   [
    "no_match",
    0.86
   ],
   [
    "open_decisions",
    0.07
   ],
   [
    "ask_needs_me",
    0.02
   ],
   [
    "start_factory",
    0.02
   ]
  ],
  "args": {},
  "latency_ms": 213,
  "tokens": 3063,
  "expected": "no_match",
  "correct": true
 },
 {
  "text": "how do I install kstrl on windows",
  "route": "no_match",
  "conf": 0.96,
  "top": [
   [
    "no_match",
    0.96
   ],
   [
    "start_factory",
    0.02
   ],
   [
    "ask_needs_me",
    0.01
   ],
   [
    "open_decisions",
    0.01
   ]
  ],
  "args": {},
  "latency_ms": 266,
  "tokens": 3070,
  "expected": "no_match",
  "correct": true
 },
 {
  "text": "email the report to the team",
  "route": "no_match",
  "conf": 0.93,
  "top": [
   [
    "no_match",
    0.94
   ],
   [
    "open_decisions",
    0.03
   ],
   [
    "decide",
    0.01
   ],
   [
    "start_factory",
    0.01
   ]
  ],
  "args": {},
  "latency_ms": 242,
  "tokens": 3067,
  "expected": "no_match",
  "correct": true
 },
 {
  "text": "retyr client-comands",
  "route": "retry",
  "conf": 0.95,
  "top": [
   [
    "retry",
    0.96
   ],
   [
    "no_match",
    0.04
   ],
   [
    "start_factory",
    0.0
   ],
   [
    "ask_cost",
    0.0
   ]
  ],
  "args": {
   "component": {
    "value": "client-commands",
    "conf": 0.99
   }
  },
  "latency_ms": 208,
  "tokens": 3067,
  "expected": "retry",
  "correct": true
 },
 {
  "text": "aprove the merge",
  "route": "decide",
  "conf": 0.99,
  "top": [
   [
    "decide",
    1.0
   ],
   [
    "open_learning",
    0.0
   ],
   [
    "open_config",
    0.0
   ],
   [
    "start_factory",
    0.0
   ]
  ],
  "args": {
   "decide_choice": {
    "value": "approve_run",
    "conf": 0.87
   },
   "target_item": {
    "value": "checkpoint: comp-c (approve PR creation and merge)",
    "conf": 0.94
   }
  },
  "latency_ms": 227,
  "tokens": 3065,
  "expected": "decide",
  "correct": true
 },
 {
  "text": "cst of live01",
  "route": "open_run",
  "conf": 0.32,
  "top": [
   [
    "open_run",
    0.36
   ],
   [
    "ask_cost",
    0.34
   ],
   [
    "no_match",
    0.22
   ],
   [
    "open_decisions",
    0.03
   ]
  ],
  "args": {
   "run": {
    "value": "live01",
    "conf": 0.99
   }
  },
  "latency_ms": 267,
  "tokens": 3067,
  "expected": "ask_cost",
  "correct": false
 },
 {
  "text": "opne failures",
  "route": "open_failures",
  "conf": 0.97,
  "top": [
   [
    "open_failures",
    0.98
   ],
   [
    "no_match",
    0.02
   ],
   [
    "open_run",
    0.0
   ],
   [
    "ask_cost",
    0.0
   ]
  ],
  "args": {},
  "latency_ms": 211,
  "tokens": 3064,
  "expected": "open_failures",
  "correct": true
 },
 {
  "text": "comp-c",
  "route": "open_component",
  "conf": 0.76,
  "top": [
   [
    "open_component",
    0.78
   ],
   [
    "decide",
    0.13
   ],
   [
    "open_decisions",
    0.04
   ],
   [
    "no_match",
    0.03
   ]
  ],
  "args": {
   "component": {
    "value": "comp-c",
    "conf": 1.0
   }
  },
  "latency_ms": 249,
  "tokens": 3063,
  "expected": "open_component",
  "correct": true
 },
 {
  "text": "merge",
  "route": "open_decisions",
  "conf": 0.27,
  "top": [
   [
    "open_decisions",
    0.31
   ],
   [
    "decide",
    0.29
   ],
   [
    "open_delivery",
    0.19
   ],
   [
    "no_match",
    0.1
   ]
  ],
  "args": {},
  "latency_ms": 214,
  "tokens": 3062,
  "expected": "decide",
  "correct": true
 },
 {
  "text": "why",
  "route": "ask_why_failed",
  "conf": 0.45,
  "top": [
   [
    "ask_why_failed",
    0.48
   ],
   [
    "no_match",
    0.32
   ],
   [
    "open_decisions",
    0.09
   ],
   [
    "ask_needs_me",
    0.08
   ]
  ],
  "args": {
   "component": {
    "value": "not named",
    "conf": 0.93
   },
   "run": {
    "value": "not named",
    "conf": 0.93
   }
  },
  "latency_ms": 256,
  "tokens": 3062,
  "expected": "no_match",
  "correct": true
 },
 {
  "text": "run",
  "route": "start_factory",
  "conf": 0.32,
  "top": [
   [
    "start_factory",
    0.35
   ],
   [
    "no_match",
    0.23
   ],
   [
    "open_run",
    0.11
   ],
   [
    "open_decisions",
    0.09
   ]
  ],
  "args": {},
  "latency_ms": 224,
  "tokens": 3062,
  "expected": "no_match",
  "correct": true
 },
 {
  "text": "heatmap of cost by hour of day",
  "route": "make_view",
  "conf": 1.0,
  "top": [
   [
    "make_view",
    1.0
   ],
   [
    "start_factory",
    0.0
   ],
   [
    "ask_needs_me",
    0.0
   ],
   [
    "toggle_theme",
    0.0
   ]
  ],
  "args": {
   "view_source": {
    "value": "costs",
    "conf": 0.99
   },
   "view_form": {
    "value": "graph",
    "conf": 0.3
   },
   "view_group": {
    "value": "none",
    "conf": 0.29
   },
   "view_sort": {
    "value": "time",
    "conf": 0.65
   },
   "view_filter_state": {
    "value": "all",
    "conf": 0.94
   },
   "period": {
    "value": "unstated",
    "conf": 0.99
   },
   "view_placement": {
    "value": "unstated",
    "conf": 1.0
   },
   "component": {
    "value": "not named",
    "conf": 0.96
   },
   "run": {
    "value": "not named",
    "conf": 0.94
   }
  },
  "latency_ms": 287,
  "tokens": 3069,
  "expected": "make_view",
  "correct": true
 },
 {
  "text": "word cloud of finding kinds",
  "route": "make_view",
  "conf": 0.96,
  "top": [
   [
    "make_view",
    0.97
   ],
   [
    "no_match",
    0.03
   ],
   [
    "open_learning",
    0.0
   ],
   [
    "retry",
    0.0
   ]
  ],
  "args": {
   "view_source": {
    "value": "findings",
    "conf": 0.96
   },
   "view_form": {
    "value": "unstated",
    "conf": 0.46
   },
   "view_group": {
    "value": "component",
    "conf": 0.4
   },
   "view_sort": {
    "value": "none",
    "conf": 0.89
   },
   "view_filter_state": {
    "value": "all",
    "conf": 0.8
   },
   "period": {
    "value": "unstated",
    "conf": 1.0
   },
   "view_placement": {
    "value": "unstated",
    "conf": 0.97
   },
   "component": {
    "value": "not named",
    "conf": 0.93
   },
   "run": {
    "value": "not named",
    "conf": 0.97
   }
  },
  "latency_ms": 244,
  "tokens": 3066,
  "expected": "make_view",
  "correct": true
 },
 {
  "text": "treemap of cost by component",
  "route": "make_view",
  "conf": 1.0,
  "top": [
   [
    "make_view",
    1.0
   ],
   [
    "start_factory",
    0.0
   ],
   [
    "ci_poll",
    0.0
   ],
   [
    "open_failures",
    0.0
   ]
  ],
  "args": {
   "view_source": {
    "value": "costs",
    "conf": 0.89
   },
   "view_form": {
    "value": "graph",
    "conf": 0.93
   },
   "view_group": {
    "value": "component",
    "conf": 1.0
   },
   "view_sort": {
    "value": "cost",
    "conf": 0.95
   },
   "view_filter_state": {
    "value": "all",
    "conf": 0.89
   },
   "period": {
    "value": "unstated",
    "conf": 1.0
   },
   "view_placement": {
    "value": "unstated",
    "conf": 0.96
   },
   "component": {
    "value": "not named",
    "conf": 0.96
   },
   "run": {
    "value": "not named",
    "conf": 0.97
   }
  },
  "latency_ms": 204,
  "tokens": 3067,
  "expected": "make_view",
  "correct": true
 }
];
window.JEV_COVERAGE = [
 {
  "text": "list of failed runs",
  "outside": 0.08,
  "form": "list",
  "labelled_outside": false
 },
 {
  "text": "board of components by state",
  "outside": 0.09,
  "form": "board",
  "labelled_outside": false
 },
 {
  "text": "timeline of the run's phases",
  "outside": 0.05,
  "form": "timeline",
  "labelled_outside": false
 },
 {
  "text": "graph of components and dependencies",
  "outside": 0.04,
  "form": "graph",
  "labelled_outside": false
 },
 {
  "text": "meter of spend against the cap",
  "outside": 0.04,
  "form": "meter",
  "labelled_outside": false
 },
 {
  "text": "sparkline of cost per run",
  "outside": 0.04,
  "form": "sparkline",
  "labelled_outside": false
 },
 {
  "text": "table of findings by severity",
  "outside": 0.04,
  "form": "table",
  "labelled_outside": false
 },
 {
  "text": "just the number of failed components",
  "outside": 0.05,
  "form": "number",
  "labelled_outside": false
 },
 {
  "text": "the diff for storage",
  "outside": 0.06,
  "form": "diff",
  "labelled_outside": false
 },
 {
  "text": "bar chart of tokens per component",
  "outside": 0.11,
  "form": "sparkline",
  "labelled_outside": false
 },
 {
  "text": "heatmap of cost by hour of day",
  "outside": 0.27,
  "form": "table",
  "labelled_outside": true
 },
 {
  "text": "sankey of tokens flowing from phase to phase",
  "outside": 0.19,
  "form": "graph",
  "labelled_outside": true
 },
 {
  "text": "calendar of runs by day",
  "outside": 0.12,
  "form": "table",
  "labelled_outside": true
 },
 {
  "text": "treemap of cost by component",
  "outside": 0.62,
  "form": "graph",
  "labelled_outside": true
 },
 {
  "text": "pie chart of spend per phase",
  "outside": 0.66,
  "form": "graph",
  "labelled_outside": true
 },
 {
  "text": "a map of which files each component touched",
  "outside": 0.2,
  "form": "graph",
  "labelled_outside": true
 },
 {
  "text": "word cloud of finding kinds",
  "outside": 0.88,
  "form": "unstated",
  "labelled_outside": true
 },
 {
  "text": "gauge cluster of every run's spend",
  "outside": 0.24,
  "form": "sparkline",
  "labelled_outside": true
 },
 {
  "text": "scatter of cost against tokens per run",
  "outside": 0.24,
  "form": "graph",
  "labelled_outside": true
 },
 {
  "text": "radar chart of the five integration criteria",
  "outside": 0.71,
  "form": "graph",
  "labelled_outside": true
 }
];
window.JEV_SUMMARY = {"model": "jev-1.13.0", "n_items": 86, "route_top1_lenient": 0.9535, "route_top1_strict": 0.8721, "arg_accuracy": 0.9659, "latency_p50_s": 0.241, "latency_p95_s": 0.288, "input_tokens_mean": 3065, "lowest_threshold_with_no_wrong_kept": 0.8};
