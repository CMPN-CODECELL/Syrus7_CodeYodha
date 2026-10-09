---------------------------- MODULE Countersign ----------------------------
(***************************************************************************)
(* Formal model of Countersign's execution protocol.                       *)
(*                                                                         *)
(* An attacker (or a compromised AI) may try to execute any order at any   *)
(* time, replay capabilities, tamper with orders after approval, crash the *)
(* executor, and drop broker responses after the broker accepted an order. *)
(*                                                                         *)
(* Safety properties checked by TLC:                                       *)
(*   NoUnapprovedOrder : the broker only ever holds countersigned orders    *)
(*   SignedVersionOnly : the broker only holds the exact signed version     *)
(*   AtMostOnce        : each approval produces at most one broker order    *)
(*                                                                         *)
(* With NAIVE = TRUE the executor retries blindly after an unknown outcome *)
(* (no reconciliation); TLC then produces a counterexample violating       *)
(* AtMostOnce, which is exactly the duplicate-order bug naive copilots have.*)
(***************************************************************************)
EXTENDS Naturals, FiniteSets

CONSTANTS Orders, MaxVersion, NAIVE

VARIABLES
    approved,       \* orders the trader countersigned
    signedVersion,  \* version of each order covered by the signature
    version,        \* current (possibly tampered) version of each order
    capUsed,        \* single-use capabilities already consumed
    exec,           \* executor state per order
    sendVersion,    \* the verified copy the executor sends (outbox), immune to later tampering
    book            \* broker order book: order -> [version -> count]

vars == <<approved, signedVersion, version, capUsed, exec, sendVersion, book>>

States == {"idle", "sending", "unknown", "acked", "blocked"}

Init ==
    /\ approved = {}
    /\ signedVersion = [o \in Orders |-> 0]
    /\ version = [o \in Orders |-> 0]
    /\ capUsed = {}
    /\ exec = [o \in Orders |-> "idle"]
    /\ sendVersion = [o \in Orders |-> 0]
    /\ book = [o \in Orders |-> [v \in 0..MaxVersion |-> 0]]

Sent(o) == book[o][0] + book[o][1] + (IF MaxVersion >= 2 THEN book[o][2] ELSE 0)

(* The trader countersigns the exact current version of an order. *)
Approve(o) ==
    /\ o \notin approved
    /\ exec[o] = "idle"
    /\ approved' = approved \cup {o}
    /\ signedVersion' = [signedVersion EXCEPT ![o] = version[o]]
    /\ UNCHANGED <<version, capUsed, exec, sendVersion, book>>

(* An attacker changes an order (before or after approval). *)
Tamper(o) ==
    /\ version[o] < MaxVersion
    /\ version' = [version EXCEPT ![o] = @ + 1]
    /\ UNCHANGED <<approved, signedVersion, capUsed, exec, sendVersion, book>>

(* Anyone may request execution of any order, any number of times (replay). *)
(* The executor consumes the capability atomically and re-verifies the hash. *)
RequestExecution(o) ==
    /\ exec[o] \in {"idle", "acked", "blocked"}
    /\ IF o \in approved /\ o \notin capUsed /\ version[o] = signedVersion[o]
         THEN /\ capUsed' = capUsed \cup {o}
              /\ exec' = [exec EXCEPT ![o] = "sending"]
              /\ sendVersion' = [sendVersion EXCEPT ![o] = version[o]]
         ELSE /\ exec' = [exec EXCEPT ![o] = IF exec[o] = "acked" THEN "acked" ELSE "blocked"]
              /\ UNCHANGED <<capUsed, sendVersion>>
    /\ UNCHANGED <<approved, signedVersion, version, book>>

(* The broker accepts and the response arrives. *)
SendOk(o) ==
    /\ exec[o] = "sending"
    /\ book' = [book EXCEPT ![o][sendVersion[o]] = @ + 1]
    /\ exec' = [exec EXCEPT ![o] = "acked"]
    /\ UNCHANGED <<approved, signedVersion, version, capUsed, sendVersion>>

(* The broker accepts but the response is lost (client times out). *)
SendAcceptedResponseLost(o) ==
    /\ exec[o] = "sending"
    /\ book' = [book EXCEPT ![o][sendVersion[o]] = @ + 1]
    /\ exec' = [exec EXCEPT ![o] = "unknown"]
    /\ UNCHANGED <<approved, signedVersion, version, capUsed, sendVersion>>

(* The request never reached the broker, or the executor crashed mid-send. *)
SendLostOrCrash(o) ==
    /\ exec[o] = "sending"
    /\ exec' = [exec EXCEPT ![o] = "unknown"]
    /\ UNCHANGED <<approved, signedVersion, version, capUsed, sendVersion, book>>

(* Recovery after an unknown outcome. Countersign reconciles the broker      *)
(* order book by unique client tag before retrying; NAIVE retries blindly. *)
Recover(o) ==
    /\ exec[o] = "unknown"
    /\ IF NAIVE
         THEN exec' = [exec EXCEPT ![o] = "sending"]
         ELSE exec' = [exec EXCEPT ![o] = IF Sent(o) > 0 THEN "acked" ELSE "sending"]
    /\ UNCHANGED <<approved, signedVersion, version, capUsed, sendVersion, book>>

Next ==
    \E o \in Orders :
        \/ Approve(o)
        \/ Tamper(o)
        \/ RequestExecution(o)
        \/ SendOk(o)
        \/ SendAcceptedResponseLost(o)
        \/ SendLostOrCrash(o)
        \/ Recover(o)

Spec == Init /\ [][Next]_vars

TypeOK ==
    /\ approved \subseteq Orders
    /\ capUsed \subseteq approved
    /\ exec \in [Orders -> States]

NoUnapprovedOrder == \A o \in Orders : Sent(o) > 0 => o \in approved

SignedVersionOnly == \A o \in Orders : \A v \in 0..MaxVersion : book[o][v] > 0 => v = signedVersion[o]

AtMostOnce == \A o \in Orders : Sent(o) <= 1
=============================================================================
