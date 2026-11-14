// Plain-English notes for the field reports, Operations (restoration, live, learn) and Asset management. Keys follow glossary.js: "Label"
// alone, "#/screen / Label" for words that mean different things on different screens. Merge with: Object.assign(INFO, INFO_FIELD).
export const INFO_FIELD = {
  "#/ops / Customers restored": "Customers whose supply is back, out of all the customers the restoring outages took off supply. Live outages count the customers the switching and crews brought back; the crews' own counts are shown separately in the field reports.",
  "#/ops / Customers off supply": "Customers still without power across the active outages, as reported by the live engine.",
  "#/ops / Automatic (FLISR)": "Customers restored automatically by the feeder automation (FLISR) that isolates a fault and closes the unfaulted section back in, before any crew arrives.",
  "#/ops / Life support still off": "Life support customers still without power on the active outages. They are restored first.",
  "#/ops / Estimated back": "The time the engine estimated, at the trip, that supply would return (the estimated time of restoration). It is not updated during the outage.",
  "#/ops / Field report": "The outcome and customers restored, as filed by the crew on the outage's repair job. It appears once a report has been filed.",
  "#/ops / Timeline": "Meter alarms, customer calls and switch readings from the live streams, newest first. Each call names its customer and is checked against the network: verified when the premises is off supply or its meter reported a loss of supply, resolved when the meter has since reported supply back, not verified otherwise; a report of wires down always needs a crew to attend.",
  "#/ops / Resource performance by crew": "For each crew: the field reports filed, the share fixed, the follow-up work needed, the customers restored, and the average minutes from the job being raised to the crew arriving on site.",
  "#/ops / Damage insights by cause": "Customer minutes off supply by cause over the last twelve months: what has cost the most customer time, not just how many outages it caused.",
  "#/assets / Review queue": "Asset photos waiting for a decision. The AI check describes what the photo shows; a person decides, and every decision needs a note.",
  "#/assets / Accept as fixed": "Closes the open defects on the tagged asset in the app. The synced defect list is not changed. Use it when the photo shows the defect repaired.",
  "#/assets / Accept as updated photo": "Makes this photo the asset's current picture. It does not close any defect.",
  "#/assets / Reject and delete": "Deletes the photo image. The record of the upload and of the decision stays for audit.",
  "#/assets / Recorded defect": "Shows whether the defects already recorded on the asset look repaired in the photo.",
  "#/assets / Compare to design": "The located asset is compared with the planned asset. A different asset is a variation from the design, which must be resolved before the record is promoted.",
  "#/assets / Promote as built": "Makes the field record the trusted as-built record. It needs validation first, and any variations must be resolved.",
  "Field report": "What a crew found and did on a job: the response, the patrol, the fault, segments and switching, customers restored, access and hazards, materials, notes, and the outcome.",
  "Segments cleared": "Feeder segments the crew confirmed clear of the fault and safe to re-energise.",
};
