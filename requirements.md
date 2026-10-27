# CD Library and Changer Manager — Requirements

## 1. Purpose

The application provides a searchable catalog of the user's CD collection and keeps track of which CDs are currently loaded in a 101-position CD changer. It must make it easy to find a CD or a song and see the CD's changer slot, if any.

## 2. Scope and assumptions

- The library contains all CDs the user owns, including discs that are not currently loaded in the changer.
- The changer has 101 positions numbered **0 through 100**, inclusive.
- A slot can contain no more than one CD, and a CD can be loaded in no more than one slot at a time.
- Unloading a CD removes its slot assignment but does not remove the CD or its metadata from the library.
- The application is intended for personal use on the user's local network and should run in a container on a Proxmox server.
- The application does not control the physical changer; the user records loading and unloading actions in the application.

## 3. Functional requirements

### 3.1 Collection management

- **FR-01 — Add CDs:** The user can add a CD to the library.
- **FR-02 — Edit CD details:** The user can view and edit a CD's metadata after it has been added.
- **FR-03 — Remove CDs:** The user can remove a CD they no longer own. The application must make clear that removal deletes its library record and must prevent accidental removal, for example with a confirmation step.
- **FR-04 — Retain unloaded CDs:** A CD that is unloaded remains in the library and can be loaded into a slot again later.
- **FR-05 — Identify CDs:** The application should support identifying an edition or release separately when needed, rather than treating every CD with the same album title as necessarily identical.

### 3.2 CD metadata

- **FR-06 — Store release details:** A CD record can store, at minimum, album/release title, artist or artists, release year/date when known, and track list.
- **FR-07 — Store track details:** Each track can store its title and track number, and may store additional available details such as duration and track artist.
- **FR-08 — Store cover art:** The user can associate cover images with a CD and view the artwork in the application.
- **FR-09 — Manual entry:** The user can enter and update CD, track, and cover-art information manually. Metadata that is unavailable must not prevent saving a record.
- **FR-10 — Import from online metadata sources:** The user can search MusicBrainz or Discogs for a release and import selected available release metadata, including its track list and cover-art reference or image when available. Integrations should use the official APIs: [MusicBrainz API](https://musicbrainz.org/doc/MusicBrainz_API) and [Discogs Developers](https://www.discogs.com/developers).
- **FR-11 — Review imported data:** Before applying an import, the user can identify the candidate release and review the information to be imported. Importing must not silently overwrite existing manually maintained data.
- **FR-12 — Import failure feedback:** If an online metadata source cannot be reached, returns no match, or has no requested information, the application clearly explains the outcome and leaves the existing record intact.

### 3.3 Changer slot management

- **FR-13 — Show all slots:** The application can display every changer position from 0 through 100 and distinguish occupied slots from empty ones.
- **FR-13a — At-a-glance loaded-CD overview:** The application provides an easy-to-read view that makes it possible to see at a glance which CDs are currently loaded and their slot numbers, while clearly distinguishing empty slots.
- **FR-14 — Load a CD:** The user can assign a library CD to an empty slot.
- **FR-15 — Unload a CD:** The user can clear a CD's slot assignment while keeping the CD in the library.
- **FR-16 — Enforce slot constraints:** The application prevents assigning a second CD to an occupied slot, assigning one CD to multiple slots, and using a slot number outside 0–100.
- **FR-17 — Show location:** Wherever a CD is shown in the library or search results, its current status is clear: loaded with its slot number, or not loaded.
- **FR-18 — Change slot:** The user can move a loaded CD to another empty slot without creating duplicate assignments.

### 3.4 Search and discovery

- **FR-19 — Search library:** The user can search the collection by album/release title, artist, or song/track title.
- **FR-20 — Show useful results:** Search results identify the matching CD and show its loaded status and slot number when loaded.
- **FR-21 — Open CD details:** The user can open a result to see the CD's metadata, cover art, track list, and changer status.
- **FR-22 — Browse the collection:** The user can browse the full collection without performing a search.
- **FR-23 — Find a song's CD:** Selecting or searching for a song identifies the CD containing that track and shows where the CD is loaded, if applicable.
- **FR-23a — Filter unloaded CDs:** The user can filter the CD library to show only CDs that are not currently loaded in the changer.

### 3.5 Collection presentation

- **FR-24 — Gallery view:** The user can browse CDs in a cover-art-focused gallery.
- **FR-25 — Album/list view:** The user can browse a compact album-oriented list of CDs.
- **FR-26 — Detailed view:** The user can browse a detailed presentation with relevant metadata and track information.
- **FR-27 — Switch views:** The user can switch between the available presentation modes without losing the current search or browsing context.
- **FR-28 — Changer view:** The user can view the changer's slots and the CDs assigned to them.

### 3.6 Appearance

- **FR-29 — Light theme:** The application provides a light theme.
- **FR-30 — Dark theme:** The application provides a dark theme.
- **FR-31 — Change theme:** The user can switch themes, and the selected theme should persist between visits for that user or browser.

## 4. Operational and quality requirements

- **NFR-01 — Web access:** The application is accessed through a web browser on the user's network; no dedicated desktop client is required.
- **NFR-02 — Container deployment:** The application can be installed and run as a container on the user's Proxmox-hosted environment using documented configuration steps.
- **NFR-03 — Persistent data:** CD records, track lists, slot assignments, and user-added cover art must survive application/container restarts and upgrades.
- **NFR-04 — Backup and restore:** The deployment documentation must describe which persistent data needs backing up and how to restore it.
- **NFR-05 — Network service dependency:** MusicBrainz and Discogs imports require network access to their relevant online services; catalog browsing, manual editing, and slot management should continue to work when either service is unavailable.
- **NFR-06 — Data integrity:** Updates to slot assignments must preserve the one-CD-per-slot and one-slot-per-CD constraints, including when operations are interrupted or repeated.
- **NFR-07 — Responsive usability:** Core search, CD detail, and slot-management screens should be usable on common desktop and mobile browser sizes.
- **NFR-08 — Accessible presentation:** Text and controls should remain legible in both themes, and interactive controls should have clear names and visible focus states.
- **NFR-09 — Clear errors:** The application provides actionable feedback for failed saves, invalid slot assignments, and metadata-import problems; it must not report a failed operation as successful.
- **NFR-10 — Local access protection:** The deployment must document how access is restricted to the intended network and whether authentication is provided by the application or an existing trusted network service.

## 5. Key user workflows

### Add a CD manually

1. The user chooses to add a CD.
2. The user enters available release and track information and optionally adds cover art.
3. The application saves the CD even if some optional metadata is unknown.
4. The new CD appears in the library as not loaded until assigned a slot.

### Add a CD using an online metadata source

1. The user chooses MusicBrainz or Discogs and searches for a release using identifying information.
2. The application presents candidate releases and their identifying details.
3. The user selects and reviews a candidate and chooses which available details to import.
4. The application saves the selected metadata without silently replacing existing information.
5. The user can edit imported details manually afterward.
6. If the selected source is unavailable or has no suitable match, the application explains this and allows the user to try the other source or enter details manually.

### Load or move a CD

1. The user selects a CD and chooses an available slot from 0–100, or starts from an empty slot and selects a CD.
2. The application rejects an occupied slot or a CD already assigned elsewhere and explains the conflict.
3. On success, the CD and slot views show the same assignment.

### Unload a CD

1. The user chooses to unload a CD or clear an occupied slot.
2. The application clears the slot assignment.
3. The CD remains in the library, marked as not loaded.

### Find a CD or song

1. The user searches by album title, artist, or song title.
2. Results identify matching CDs and show whether each is loaded and its slot number.
3. The user can open a CD to inspect its full track list and metadata.

## 6. Acceptance criteria

- The application can represent all 101 slots, including both slot 0 and slot 100.
- The changer overview makes occupied and empty slots easy to distinguish and shows each loaded CD with its slot number.
- It is possible to add a CD with manually entered album and track information, find it, load it into an empty slot, and see that slot in both search results and the changer view.
- It is possible to unload that CD, see its slot become empty, and still find the CD in the library as not loaded.
- The application prevents an occupied slot from receiving another CD and prevents one CD from being assigned to two slots.
- Searching by an album title, artist, or track title can find the associated CD; the result identifies its loaded slot or clearly says it is not loaded.
- A CD detail view shows its available metadata, cover art, track list, and load status.
- The user can switch among gallery, album/list, and detailed views, and use both light and dark themes.
- MusicBrainz and Discogs releases can each be searched for and reviewed before import. No-match or service-unavailable cases provide clear feedback and do not damage existing metadata.
- Library records, cover art, and slot assignments remain present after an application/container restart.
- Deployment instructions cover starting the container, preserving its data, backing up/restoring that data, and limiting access to the intended network.

## 7. Suggested delivery boundary

### Initial release

Include collection CRUD, manual metadata and cover-art entry, MusicBrainz and Discogs lookup/import, slot assignment and unloading, album/artist/track search, gallery/list/detail presentations, light and dark themes, and container deployment with persistent storage.

### Possible future enhancements

These are not required for the initial release unless separately prioritized:

- Barcode scanning or optical-disc identification.
- Automatic recognition of the physical changer's current contents.
- Multiple libraries, users, or changers.
- Advanced filters, playlists, listening history, or statistics.
- Additional online metadata providers or scheduled metadata refresh.
- Bulk import/export and integrations with other collection-management software.
