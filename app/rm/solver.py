"""The roster engine: a fast greedy weighted assignment, so re-rostering is instant on screen (production would run the same model
as a CP-SAT solve on Databricks compute). One weighting balances the score across safety, cost, schedule, customer and travel.

Hard guards: the crew has the job's skill and a valid (not expired) ticket for it, is not on leave or in training, has no open
safety incident (unless a supervisor authorised them), and stays inside the enterprise agreement daily cap (unless a supervisor
authorised overtime, up to 4 hours more, which then counts as a breach)."""
import math
from collections import defaultdict

WEIGHTS = dict(safety=0.22, cost=0.18, schedule=0.18, customer=0.17, travel=0.25)   # how much each part of the score counts
SPEED_KMH = 42.0   # urban and regional blend; the field app re-times the day on real roads
OT_MIN = 240.0     # authorised overtime beyond the daily cap


def km(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(a))


class RosterEngine:
    def __init__(self, data):
        self.data = data
        self.eba = {x["role"]: x for x in data["eba_rules"]}
        self.skills = defaultdict(set)
        for s in data["skills"]:
            self.skills[s["crew_id"]].add(s["skill_code"])
        self.valid_tickets, self.expired_tickets = defaultdict(set), defaultdict(set)
        for c in data["certs"]:
            (self.expired_tickets if c["is_expired"] else self.valid_tickets)[c["crew_id"]].add(c["ticket_code"])
        self.on_leave = {x["crew_id"] for x in data["leave"]}
        self.in_training = {x["crew_id"] for x in data["training"]}
        self.incident = {x["crew_id"] for x in data["incidents"] if x.get("crew_id")}
        self.crews = {c["crew_id"]: c for c in data["crews"]}
        self.regions = [r["region_id"] for r in data["regions"]]

    def cap_min(self, crew):
        return float(self.eba.get(crew["primary_skill"], {}).get("max_hours_per_day", 12)) * 60

    def crew_for_region(self, region, allow_incident=None, exclude_crew=None):
        """Crews that can be rostered in a region today. `allow_incident` are open-incident crews a supervisor authorised back in;
        `exclude_crew` are crews the auto-solve must not hand work to (just moved in, awaiting a deliberate dispatch)."""
        allow, ex = allow_incident or set(), exclude_crew or set()
        return [c for c in self.data["crews"] if c["region_id"] == region and c["crew_id"] not in self.on_leave
                and c["crew_id"] not in self.in_training and c["crew_id"] not in ex
                and (c["crew_id"] not in self.incident or c["crew_id"] in allow)]

    def jobs_for_region(self, region, exclude_jobs=None):
        """Parked jobs are off the board until a dispatcher re-dispatches them, so they are never also on a crew's day."""
        ex = exclude_jobs or set()
        return [j for j in self.data["jobs"] if j["region_id"] == region and j["job_id"] not in ex]

    def eligible(self, job, crew):
        return job["required_skill"] in self.skills.get(crew["crew_id"], set()) and job["required_ticket"] in self.valid_tickets.get(crew["crew_id"], set())

    def solve(self, region, allow_incident=None, allow_fatigue=None, exclude_jobs=None, exclude_crew=None):
        weights = WEIGHTS
        crews = self.crew_for_region(region, allow_incident, exclude_crew)
        jobs = self.jobs_for_region(region, exclude_jobs)
        allow_fat = allow_fatigue or set()
        state = {}
        for c in crews:
            cap = self.cap_min(c)
            state[c["crew_id"]] = dict(used_min=0.0, lat=c["home_lat"], lon=c["home_lon"], eba_min=cap,
                                       max_min=cap + OT_MIN if c["crew_id"] in allow_fat else cap, jobs=[])
        # placement order: whatever goes first is least likely to be deferred when crew hours run out
        order = sorted(jobs, key=lambda j: (-j["priority"], -j["est_customer_minutes"]))
        assignments, deferred, blocked_by_expiry = [], [], set()
        for job in order:
            best, best_score = None, 1e18
            for c in crews:
                cid = c["crew_id"]
                if (job["required_skill"] in self.skills.get(cid, set()) and job["required_ticket"] in self.expired_tickets.get(cid, set())
                        and job["required_ticket"] not in self.valid_tickets.get(cid, set())):
                    blocked_by_expiry.add(cid)
                if not self.eligible(job, c):
                    continue
                st = state[cid]
                travel_min = km(st["lat"], st["lon"], job["lat"], job["lon"]) / SPEED_KMH * 60.0
                need = job["duration_min"] + travel_min
                if st["used_min"] + need > st["max_min"]:
                    continue
                cost = c["hourly_cost_aud"] * need / 60.0
                fatigue = st["used_min"] / st["max_min"]
                slack = max(0.0, (job["window_end_min"] - job["window_start_min"]) - need)
                # sub-scores each span about 0 to 1 for a typical job, so each weight genuinely changes which crew wins
                score = (weights["cost"] * cost / 1200.0 + weights["travel"] * travel_min / 90.0
                         + weights["safety"] * (fatigue + (0.55 if c["employment_type"] == "Contractor" else 0.0))
                         + weights["schedule"] * (1.0 - min(1.0, slack / 480.0))
                         + weights["customer"] * (job["est_customer_minutes"] / 900.0) * (0.5 + fatigue))
                if score < best_score:
                    best, best_score = c, score
            if best is None:
                deferred.append(job)
                continue
            st = state[best["crew_id"]]
            travel_km = km(st["lat"], st["lon"], job["lat"], job["lon"])
            travel_min = travel_km / SPEED_KMH * 60.0
            st["used_min"] += job["duration_min"] + travel_min
            st["lat"], st["lon"] = job["lat"], job["lon"]
            st["jobs"].append(job["job_id"])
            hours = (job["duration_min"] + travel_min) / 60.0
            cost = best["hourly_cost_aud"] * hours
            assignments.append(dict(job_id=job["job_id"], job_type=job["job_type"], title=job["title"], priority=job["priority"],
                                    crew_id=best["crew_id"], crew_name=best["crew_name"], employment_type=best["employment_type"],
                                    region_id=region, lat=job["lat"], lon=job["lon"], travel_km=round(travel_km, 1), travel_min=round(travel_min),
                                    duration_min=job["duration_min"], funding=job["funding"], est_customer_minutes=job["est_customer_minutes"],
                                    is_emergency=bool(job.get("is_emergency")), cost_aud=round(cost, 2)))
        return dict(region=region, assignments=assignments, deferred=deferred,
                    kpis=self.kpis(assignments, deferred, state, len(blocked_by_expiry)))

    def kpis(self, assignments, deferred, state, expired_blocks):
        cost = sum(a["cost_aud"] for a in assignments)
        travel = sum(a["travel_km"] for a in assignments)
        active = {cid: s for cid, s in state.items() if s["jobs"]}
        return dict(jobs_assigned=len(assignments), jobs_deferred=len(deferred), crew_deployed=len(active),
                    total_cost_aud=round(cost), total_travel_km=round(travel),
                    deferred_customer_minutes=sum(j["est_customer_minutes"] for j in deferred), scope3_co2_kg=round(travel * 0.21),
                    # real breaches: a deployed crew past its cap (only with authorised overtime) and open-incident crews rostered
                    eba_breaches=sum(1 for s in active.values() if s["used_min"] > s["eba_min"] + 1e-6),
                    open_incident_crew=sum(1 for cid in active if cid in self.incident),
                    expired_ticket_blocks=expired_blocks, emergency_jobs=sum(1 for a in assignments if a["is_emergency"]))
