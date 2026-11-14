// Glossary for the customer notices (notify.js) and the Customers screen (customers.js). Merged into glossary.js's INFO by the lead:
// import { INFO as NOTIFY_INFO } from "./glossary_notify.js" and spread it in. Keys follow glossary.js: a plain label, or "Panel title / Label",
// or "#/screen / Label" when the same word means something else on another screen.
export const INFO = {
  // ---- customer notices: the figures across the top
  "Drafts": "Notices being designed. The AI writes the first draft of each chosen channel; a person edits it before it is released.",
  "Scheduled": "Notices released to go at a set time: a planned interruption's notice goes at 9 am on its notice date. Nothing reaches a customer before then.",
  "Sent this week": "Notices sent in the last seven days, and the customers they reached.",
  "Works plan": "The works plan the notice belongs to, from Plan and invest: either a released plan or a programme still being designed. Select it to open the plan. A notice whose plan has since been deleted shows that the works plan was removed from the registry; the notice is kept as a record.",

  // ---- the wizard and what is on the screen
  "Notice period": "A planned interruption needs four business days' notice before it starts (public holidays are not counted). Met means there is still time to tell the customers; too soon means the notice date has passed.",
  "Customers impacted": "Customers off supply for the planned outage: the customers of the distribution substations behind its isolation point.",
  "Work items": "The planned jobs and open defect repairs done in the outage.",
  "Customers off": "Customers without supply on this outage, as the live engine counts them.",
  "Life support off": "Life support customers without supply on this outage.",
  "Choose the channels": "Each customer is reached on their registered preferred channel if it is chosen. Otherwise they are reached on another chosen channel they have opted into, or by letter if Mail is chosen. A customer whom none of the chosen channels reaches is left out and counted.",
  "Business rules applied": "Each rule the notice passed through and how many customers it changed.",
  "Who it reaches": "Customers reached, life support customers and the messages each channel carries. Select a count to see the customers. Customers left out are counted under the rule that excluded them.",
  "Drafted message": "The text for each audience and chosen channel. Edit any of it; each edit is logged with who made it and when.",
  "Reached by": "The channels the notice reaches this customer on.",
  "Released customer notices / Goes or went": "When the notice is scheduled to go, or was sent. SMS and calls wait until 8 am in quiet hours.",

  // ---- customers: connection applications
  "Stuck": "An open application that is held by a blocker, or has been longer in its stage than the stage allows (the limit is shown on the application).",
  "Days in stage": "Days since the application entered its current stage.",
  "Days past target": "Days past the connection date promised at application, for an application still open.",
  "Awaiting payment": "Open applications with a quote or an invoice not yet paid.",
  "Blockers by stage": "The reason each open application is held, and the stage it is in.",
  "Payment": "Payment status of the application: not due yet, quoted, invoiced, paid, or no charge (distributed energy connections that need no network work).",
  "Provider": "The accredited service provider appointed to the work. Blank until one is appointed.",
  "Target": "The connection date promised at application.",
  "Blocker": "The reason the application is held in its current stage.",

  // ---- customers: existing customers
  "Premises": "Premises on the register (one per NMI).",
  "Life support": "Premises on the life support register (registered or pending). Life support customers get a contact centre call with every notice.",
  "Critical sites": "Hospitals, aged care, schools, water and telecom sites on the register.",
  "Distributed energy": "Solar, batteries and EV chargers registered at the premises.",
  "Off supply now": "Premises whose protective device has a live outage now.",
  "Reachable by text": "Premises registered for SMS. The rest are reached by email, app or web, or by mail if they have no digital channel.",
  "Supply": "On supply, or off supply now: the device that protects the premises has a live outage.",
  "Customers interrupted": "Customers interrupted by the outage on the device.",
  "#/customers / Customer type": "Residential, small business or large business, from the customer register.",
};

export default INFO;
