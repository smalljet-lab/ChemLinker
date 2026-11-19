/******************************************************************************************************
Compound Registry add-on for MestReNova.

Links the NMR spectra of a document to a compound ID of the local compound registry and saves the
document as <compound folder>/<ID>_<sample>.mnova. The compound folder is the one the registry made when
the ID was created (<registry>/compounds/<compound>, see files.folder in the registry's config.toml).

"Save Report and PDF to Compound" saves the linked document again, writes <ID>_<sample>.pdf next to it
(all pages as they look but without the spectrum title and comment, then one page per spectrum with its
shift report and the parameters) and stores
the shift report and the parameters of every spectrum of the document (1H, 13C, 19F, 31P ..., 2D) with the
compound in the registry. The shift report is the multiplet report; a spectrum without multiplets that is
not 1H (typically 13C) gets the peak list report instead. Both are written by MestReNova's own reporters.

"Paste Structure on Spectrum" puts the registered structure of the linked compound on the current page.

"Compound Library..." lists the compounds of the registry (scrollable, with a search field) and opens the
MestReNova document saved for the chosen compound, as a document of its own next to the user's spectrum.

The ID is never guessed: the add-on reads the spectrum title (the comment typed at the spectrometer),
looks for the number of an ELN experiment the registry knows (e.g. ABC-006) and shows the compounds
registered for that experiment. The chemist clicks the right structure. When the title does not name the
experiment, the chemist types an experiment number or a compound ID in the search field of the same dialog.

Installed and brought up to date by the installer of the compound registry (compound-registry setup) or by
	python -m compound_registry.mnova install
which writes into the copy the Python to start, the program version and, when asked, a registry folder. Do
not edit the installed copy. The add-on sends its version with every call; the registry program refuses an
add-on of another version and says how to update it.
*****************************************************************************************************/

/*globals Application, mainWindow, MnUi, nmr, NMRSpectrum, Attachment, serialization, settings, File, Dir,
  TextStream, Process, Timer, MessageBox, molecule, Molecule, PeakReporter, Peak, Peaks, MultipletReporter, TextDocument, draw, print*/
/*jslint plusplus: true, indent: 4*/

var compoundRegistry = {
	python: "@@COMPOUND_REGISTRY_PYTHON@@",
	home: "@@COMPOUND_REGISTRY_HOME@@",
	version: "@@COMPOUND_REGISTRY_VERSION@@",
	attachmentName: "CompoundRegistry",
	autoKey: "CompoundRegistry/ProposeOnImport",
	maxButtons: 8,
	libraryKey: "CompoundRegistry/LibraryOnlyWithNmr",
	pending: [],
	declined: {},
	timer: null,
	dialog: null
};

compoundRegistry.dialogUi = function () {
	"use strict";
	var i, buttons = "";
	for (i = 0; i < compoundRegistry.maxButtons; i++) {
		buttons += '<item row="' + Math.floor(i / 4) + '" column="' + (i % 4) + '"><widget class="QToolButton" name="tb' + i + '">' +
			'<property name="toolButtonStyle"><enum>Qt::ToolButtonTextUnderIcon</enum></property>' +
			'<property name="iconSize"><size><width>220</width><height>150</height></size></property>' +
			'<property name="autoRaise"><bool>false</bool></property></widget></item>';
	}
	return '<?xml version="1.0" encoding="UTF-8"?><ui version="4.0"><class>Dialog</class><widget class="QDialog" name="Dialog">' +
		'<property name="windowTitle"><string>Compound ID</string></property>' +
		'<layout class="QVBoxLayout" name="mainLayout">' +
		'<item><widget class="QLabel" name="lblIntro"><property name="wordWrap"><bool>true</bool></property><property name="textFormat"><enum>Qt::RichText</enum></property></widget></item>' +
		'<item><layout class="QGridLayout" name="grid">' + buttons + '</layout></item>' +
		'<item><layout class="QHBoxLayout" name="moreLayout">' +
		'<item><widget class="QLabel" name="lblMore"><property name="text"><string>More compounds:</string></property></widget></item>' +
		'<item><widget class="QComboBox" name="cbMore"><property name="sizePolicy"><sizepolicy hsizetype="Expanding" vsizetype="Fixed"><horstretch>1</horstretch><verstretch>0</verstretch></sizepolicy></property></widget></item>' +
		'<item><widget class="QPushButton" name="pbMore"><property name="text"><string>Use selected</string></property></widget></item>' +
		'</layout></item>' +
		'<item><layout class="QHBoxLayout" name="searchLayout">' +
		'<item><widget class="QLabel" name="lblSearch"><property name="text"><string>Another compound:</string></property></widget></item>' +
		'<item><widget class="QLineEdit" name="leSearch"><property name="placeholderText"><string>experiment number (e.g. ABC-006) or compound ID</string></property></widget></item>' +
		'<item><widget class="QPushButton" name="pbSearch"><property name="text"><string>Search</string></property><property name="default"><bool>true</bool></property></widget></item>' +
		'</layout></item>' +
		'<item><layout class="QHBoxLayout" name="folderLayout">' +
		'<item><widget class="QLabel" name="lblFolder"><property name="sizePolicy"><sizepolicy hsizetype="Expanding" vsizetype="Preferred"><horstretch>1</horstretch><verstretch>0</verstretch></sizepolicy></property></widget></item>' +
		'</layout></item>' +
		'<item><layout class="QHBoxLayout" name="bottomLayout">' +
		'<item><widget class="QCheckBox" name="chkAuto"><property name="text"><string>Propose automatically when a spectrum is opened</string></property></widget></item>' +
		'<item><spacer name="sp"><property name="orientation"><enum>Qt::Horizontal</enum></property></spacer></item>' +
		'<item><widget class="QPushButton" name="pbCancel"><property name="text"><string>Not now</string></property></widget></item>' +
		'</layout></item>' +
		'</layout></widget><resources/><connections/></ui>';
};

compoundRegistry.readText = function (aPath) {
	"use strict";
	var f = new File(aPath), s, text;
	if (!f.open(File.ReadOnly)) {
		return null;
	}
	s = new TextStream(f);
	text = s.readAll();
	f.close();
	return text;
};

compoundRegistry.writeText = function (aPath, aText) {
	"use strict";
	var f = new File(aPath), s;
	if (!f.open(File.WriteOnly)) {
		return false;
	}
	s = new TextStream(f);
	try { s.codec = "UTF-8"; } catch (ignore) {}
	s.write(aText);
	s.flush();
	f.close();
	return true;
};

compoundRegistry.html = function (aText) {
	"use strict";
	return String(aText).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/\n/g, "<br/>");
};

compoundRegistry.autoPropose = function () {
	"use strict";
	return String(settings.value(compoundRegistry.autoKey, "true")) !== "false";
};

//! Path with forward slashes and no slash at the end.
compoundRegistry.slashes = function (aPath) {
	"use strict";
	return String(aPath || "").replace(/\\/g, "/").replace(/\/+$/, "");
};

/** What the registry keeps for a compound: {path (its folder, made by the registry if it is missing), molfile}
	or null after telling the user why there is none. */
compoundRegistry.compoundInfo = function (aCoreStereo) {
	"use strict";
	var answer = compoundRegistry.runBridge("folder", compoundRegistry.jsonAscii({core_stereo_id: String(aCoreStereo || "")}),
		"Looking up the compound...");
	if (!answer.ok || !answer.result || !answer.result.path) {
		MessageBox.critical("Compound registry: the folder of " + aCoreStereo + " is not available.\n" +
			(answer.error && answer.error.message ? answer.error.message : "unknown error"));
		return null;
	}
	return {path: compoundRegistry.slashes(answer.result.path), molfile: String(answer.result.molfile || "")};
};

//! The folder the registry keeps for a compound, or "" after telling the user why there is none.
compoundRegistry.compoundFolder = function (aCoreStereo) {
	"use strict";
	var info = compoundRegistry.compoundInfo(aCoreStereo);
	return info ? info.path : "";
};

//! All NMR spectra of the document, the active one first.
compoundRegistry.spectra = function (aDoc) {
	"use strict";
	var i, item, list = [], n = aDoc.itemCount("NMR Spectrum"), active;
	try { active = nmr.activeSpectrum(); } catch (ignore) {}
	for (i = 0; i < n; i++) {
		item = new NMRSpectrum(aDoc.item(i, "NMR Spectrum"));
		if (item.isValid()) {
			if (active && active.isValid && active.isValid() && active.uuid === item.uuid) {
				list.unshift(item);
			} else {
				list.push(item);
			}
		}
	}
	return list;
};

//! What a spectrum says about its sample: title and comment (not the file path).
compoundRegistry.spectrumText = function (aSpec) {
	"use strict";
	var parts = [], seen = {};
	function add(aValue) {
		var text;
		if (aValue === undefined || aValue === null) {
			return;
		}
		text = String(aValue).replace(/\r\n?/g, "\n").replace(/^\s+|\s+$/g, "");
		if (text && !seen[text]) {
			seen[text] = true;
			parts.push(text);
		}
	}
	try { add(aSpec.title); } catch (e1) {}
	try { add(aSpec.getParam("Title")); } catch (e2) {}
	try { add(aSpec.comment); } catch (e3) {}
	try { add(aSpec.getParam("Comment")); } catch (e4) {}
	return parts.join("\n");
};

compoundRegistry.documentText = function (aDoc) {
	"use strict";
	var i, text, parts = [], seen = {}, specs = compoundRegistry.spectra(aDoc);
	for (i = 0; i < specs.length; i++) {
		text = compoundRegistry.spectrumText(specs[i]);
		if (text && !seen[text]) {
			seen[text] = true;
			parts.push(text);
		}
	}
	return {text: parts.join("\n\n"), count: specs.length};
};

//! The link stored in the document, or null.
compoundRegistry.linkOf = function (aDoc) {
	"use strict";
	var att;
	try {
		att = aDoc.getAttachment(compoundRegistry.attachmentName);
		if (att) {
			return att.getMetaData();
		}
	} catch (ignore) {}
	return null;
};

//! JSON with every non-ASCII character escaped (the temporary files are then plain ASCII).
compoundRegistry.jsonAscii = function (aObj) {
	"use strict";
	return JSON.stringify(aObj).replace(/[\u007f-\uffff]/g, function (aChar) {
		return "\\u" + ("0000" + aChar.charCodeAt(0).toString(16)).slice(-4);
	});
};

compoundRegistry.bridge = function (aText) {
	"use strict";
	return compoundRegistry.runBridge("propose", aText, "Looking up the compound ID...");
};

/** Runs the Python bridge: aCommand is "propose", "search", "library", "record" or "folder", aInput the content of its input file.
	Returns {ok: true, result: ...} or {ok: false, error: {message: ...}}. */
compoundRegistry.runBridge = function (aCommand, aInput, aBusyText) {
	"use strict";
	var dir = Dir.temp() + "/compound-registry-mnova", stamp = Application.createUuid().replace(/[\{\}]/g, ""),
		textFile = dir + "/" + aCommand + "-" + stamp + ".txt", outFile = dir + "/answer-" + stamp + ".json",
		args, proc, answer, parsed, timedOut = false, notStarted = false,
		repair = "Run the installer of the compound registry again (compound-registry setup), then restart MestReNova.";
	if (compoundRegistry.python.indexOf("@@") === 0) {
		return {ok: false, error: {message: "This copy of the add-on was not installed by the compound registry.\n" + repair}};
	}
	if (compoundRegistry.python.indexOf("/") !== -1 && !File.exists(compoundRegistry.python)) {
		return {ok: false, error: {message: "The compound registry program is not where the add-on expects it:\n" + compoundRegistry.python +
			"\nIts folder was moved or removed.\n" + repair}};
	}
	new Dir(Dir.temp()).mkpath(dir);
	if (!compoundRegistry.writeText(textFile, aInput)) {
		return {ok: false, error: {message: "Could not write " + textFile}};
	}
	args = ["-m", "compound_registry.mnova", aCommand];
	if (aCommand === "propose") {
		args.push("--text-file", textFile);
	} else {
		args.push("--data-file", textFile);
	}
	if (aCommand === "propose" || aCommand === "search" || aCommand === "library") {
		args.push("--image-dir", dir + "/structures");
	}
	args.push("--out", outFile);
	if (compoundRegistry.home && compoundRegistry.home.indexOf("@@") !== 0) {
		args.push("--home", compoundRegistry.home);
	}
	if (compoundRegistry.version && compoundRegistry.version.indexOf("@@") !== 0) {
		args.push("--addon-version", compoundRegistry.version);
	}
	proc = new Process();
	try {
		mainWindow.setBusy(aBusyText);
		proc.start(compoundRegistry.python, args);
		if (!proc.waitForFinished(60000)) {
			// a program that cannot be started also ends the wait with false, at once
			if (proc.errorCode === Process.FailedToStart) {
				notStarted = true;
			} else {
				timedOut = true;
				// stop the program, so that it cannot change the registry after the user was told it failed
				try { proc.kill(); } catch (e1) {}
				try { proc.waitForFinished(3000); } catch (e2) {}
			}
		}
	} finally {
		mainWindow.endBusy();
	}
	answer = timedOut || notStarted ? null : compoundRegistry.readText(outFile);
	try { File.remove(textFile); } catch (e3) {}
	try { File.remove(outFile); } catch (e4) {}
	if (notStarted) {
		return {ok: false, error: {message: "The compound registry program could not be started:\n" + compoundRegistry.python +
			"\nIf the compound registry was updated or moved: " + repair}};
	}
	if (timedOut) {
		return {ok: false, error: {message: "The compound registry did not answer within 60 s (" + compoundRegistry.python + ")."}};
	}
	if (!answer) {
		return {ok: false, error: {message: "The compound registry program could not be started or gave no answer.\n" +
			compoundRegistry.python + "\n" + String(proc.allErrorOutput).substring(0, 500) +
			"\nIf the compound registry was updated or moved: " + repair}};
	}
	try {
		parsed = JSON.parse(answer);
	} catch (e) {
		return {ok: false, error: {message: "Unreadable answer from the compound registry: " + e}};
	}
	return parsed;
};

//! The candidates of a "propose" or "search" answer as one list, each with the match it belongs to.
compoundRegistry.candidatesOf = function (aResult) {
	"use strict";
	var list = [], i, j, m;
	for (i = 0; i < aResult.matches.length; i++) {
		m = aResult.matches[i];
		for (j = 0; j < m.candidates.length; j++) {
			m.candidates[j].match = {experiment_ref: m.experiment_ref, sample: m.sample, file_label: m.file_label};
			list.push(m.candidates[j]);
		}
	}
	return list;
};

/** Shows the candidates and the search field. aNote (HTML) replaces the sentence about the experiment found
	in the title. Returns the chosen candidate, {search: "typed text"} when the user searched, or null. */
compoundRegistry.choose = function (aText, aCandidates, aFilesFolder, aNote, aSearchText) {
	"use strict";
	var ui = Application.loadUi(compoundRegistry.dialogUi()), w = ui.widgets, i, c, label, chosen = null,
		shown = Math.min(aCandidates.length, compoundRegistry.maxButtons), refs = [], seen = {};

	function pick(aIndex) {
		return function () {
			chosen = aCandidates[aIndex];
			ui.accept();
		};
	}

	for (i = 0; i < aCandidates.length; i++) {
		if (!seen[aCandidates[i].match.experiment_ref]) {
			seen[aCandidates[i].match.experiment_ref] = true;
			refs.push(aCandidates[i].match.experiment_ref);
		}
	}
	w.lblIntro.text = "<b>Spectrum title</b><br/>" + compoundRegistry.html(aText) + "<br/><br/>" +
		(aNote || "Experiment <b>" + compoundRegistry.html(refs.join(", ")) + "</b> found in the title. Click the compound this sample is.");

	for (i = 0; i < compoundRegistry.maxButtons; i++) {
		if (i < shown) {
			c = aCandidates[i];
			label = c.full_id + "\n" + c.match.experiment_ref + ", product " + c.product_index + (c.formula ? "  " + c.formula : "");
			if (c.name) {
				label += "\n" + (c.name.length > 38 ? c.name.substring(0, 36) + "..." : c.name);
			}
			w["tb" + i].text = label;
			w["tb" + i].toolTip = c.full_id + (c.name ? "\n" + c.name : "") + "\n" + c.smiles;
			if (c.image) {
				w["tb" + i].setIcon(c.image, 220, 150);
			}
			w["tb" + i].clicked.connect(pick(i));
		} else {
			w["tb" + i].visible = false;
		}
	}
	if (aCandidates.length > shown) {
		for (i = shown; i < aCandidates.length; i++) {
			c = aCandidates[i];
			w.cbMore.addItem(c.full_id + "  (" + c.match.experiment_ref + ", product " + c.product_index + (c.name ? ", " + c.name : "") + ")");
		}
		w.pbMore.clicked.connect(function () {
			chosen = aCandidates[shown + w.cbMore.currentIndex];
			ui.accept();
		});
	} else {
		w.lblMore.visible = false;
		w.cbMore.visible = false;
		w.pbMore.visible = false;
	}
	function search() {
		var typed = String(w.leSearch.text).replace(/^\s+|\s+$/g, "");
		if (typed) {
			chosen = {search: typed};
			ui.accept();
		}
	}
	w.leSearch.text = aSearchText || "";
	w.pbSearch.clicked.connect(search);
	if (!aCandidates.length) {
		try { w.leSearch.setFocus(); } catch (ignore) {}
	}
	w.lblFolder.text = aFilesFolder ? "Saves to the compound's folder in " + aFilesFolder : "";
	w.chkAuto.checked = compoundRegistry.autoPropose();
	w.pbCancel.clicked.connect(function () { ui.reject(); });

	compoundRegistry.dialog = ui;
	try {
		compoundRegistry.execDialog(ui);
	} finally {
		compoundRegistry.dialog = null;
	}
	settings.setValue(compoundRegistry.autoKey, w.chkAuto.checked ? "true" : "false");
	return chosen;
};

//! <compound folder>/<ID>_<sample>.mnova; never the name of another existing file.
compoundRegistry.targetPath = function (aDoc, aDir, aCandidate) {
	"use strict";
	var dir = compoundRegistry.slashes(aDir),
		stem = aCandidate.full_id + (aCandidate.match.file_label ? "_" + aCandidate.match.file_label : ""),
		path = dir + "/" + stem + ".mnova", n = 1, current = String(aDoc.name || "").replace(/\\/g, "/").toLowerCase();
	while (File.exists(path) && path.toLowerCase() !== current) {
		n++;
		path = dir + "/" + stem + "_" + n + ".mnova";
	}
	return {dir: dir, path: path};
};

//! Writes the link into the active document and saves it. Returns the path or "".
compoundRegistry.linkAndSave = function (aDoc, aCandidate, aText) {
	"use strict";
	var folder = compoundRegistry.compoundFolder(aCandidate.core_stereo_id), target, att;
	if (!folder) {
		return "";
	}
	target = compoundRegistry.targetPath(aDoc, folder, aCandidate);
	att = new Attachment();
	att.setMetaData({
		compound_id: aCandidate.full_id,
		core_stereo_id: aCandidate.core_stereo_id,
		experiment: aCandidate.match.experiment_ref,
		product_index: aCandidate.product_index,
		sample: aCandidate.match.sample,
		file_label: aCandidate.match.file_label || "",
		name: aCandidate.name || "",
		formula: aCandidate.formula || "",
		smiles: aCandidate.smiles,
		inchikey: aCandidate.inchikey,
		linked_at: new Date().toISOString()
	});
	att.setTextData(aText);
	if (!aDoc.setAttachment(compoundRegistry.attachmentName, att)) {
		MessageBox.critical("Could not store the compound ID in the document.");
		return "";
	}
	serialization.save(target.path, "mnova");
	if (!File.exists(target.path)) {
		MessageBox.critical("The document could not be saved as\n" + target.path);
		return "";
	}
	try { mainWindow.setStatusMessage("Linked to " + aCandidate.full_id + ", saved as " + target.path); } catch (ignore) {}
	return target.path;
};

/** Proposes compound IDs for the active document.
	aInteractive: true when the user asked for it (then every problem is reported); false after an import
	(then the add-on stays silent unless it has something to propose). */
compoundRegistry.propose = function (aInteractive) {
	"use strict";
	var doc = mainWindow.activeDocument, info, link, answer, candidates, chosen, path, note = "", typed = "", h = compoundRegistry.html;
	if (!doc || !doc.isValid()) {
		if (aInteractive) { MessageBox.information("Open a spectrum first."); }
		return "";
	}
	link = compoundRegistry.linkOf(doc);
	if (link && link.compound_id) {
		if (!aInteractive) {
			return "";
		}
		if (MessageBox.question("This document is already linked to " + link.compound_id + ".\nLink it to another compound?",
				MessageBox.Yes, MessageBox.No) !== MessageBox.Yes) {
			return "";
		}
	}
	info = compoundRegistry.documentText(doc);
	if (!info.count) {
		if (aInteractive) { MessageBox.information("The active document has no NMR spectrum."); }
		return "";
	}
	answer = compoundRegistry.bridge(info.text);
	if (!answer.ok) {
		if (aInteractive) {
			MessageBox.critical("Compound registry: " + (answer.error && answer.error.message ? answer.error.message : "unknown error"));
		} else {
			print("Compound registry: " + JSON.stringify(answer.error));
		}
		return "";
	}
	candidates = compoundRegistry.candidatesOf(answer.result);
	if (!candidates.length) {
		if (!aInteractive) {
			return "";
		}
		note = "No registered experiment number was found in the title. Search for the experiment number or the compound ID below.";
	}
	while (true) {
		chosen = compoundRegistry.choose(info.text, candidates, answer.result.files_folder, note, typed);
		if (!chosen) {
			compoundRegistry.declined[doc.uuid] = true;
			return "";
		}
		if (!chosen.search) {
			break;
		}
		typed = chosen.search;
		answer = compoundRegistry.runBridge("search", compoundRegistry.jsonAscii({query: typed, text: info.text}), "Searching the compound registry...");
		if (!answer.ok) {
			MessageBox.critical("Compound registry: " + (answer.error && answer.error.message ? answer.error.message : "unknown error"));
			return "";
		}
		candidates = compoundRegistry.candidatesOf(answer.result);
		if (candidates.length) {
			note = "Search <b>" + h(typed) + "</b>: " + candidates.length + (candidates.length === 1 ? " compound" : " compounds") +
				(answer.result.truncated ? " shown, there are more (type more of the number)" : "") + ". Click the compound this sample is.";
		} else {
			note = "Nothing found for <b>" + h(typed) + "</b>. The registry knows " + answer.result.known_experiments +
				" experiment(s) with an ID. Create the ID in the ELN first, or try another number.";
		}
	}
	if (mainWindow.activeDocument.uuid !== doc.uuid) {
		MessageBox.warning("The active document changed. Nothing was saved; use Link Spectrum to Compound ID again.");
		return "";
	}
	path = compoundRegistry.linkAndSave(doc, chosen, info.text);
	return path;
};

//! Event handler: a spectrum was imported. The proposal is shown once the import has finished.
compoundRegistry.onSpectrumImported = function (aJSONString) {
	"use strict";
	var doc;
	try {
		if (!compoundRegistry.autoPropose() || !mainWindow.hasGui) {
			return;
		}
		doc = mainWindow.activeDocument;
		if (!doc || !doc.isValid()) {
			return;
		}
		if (compoundRegistry.pending.indexOf(doc.uuid) === -1) {
			compoundRegistry.pending.push(doc.uuid);
		}
		if (!compoundRegistry.timer) {
			compoundRegistry.timer = new Timer();
			compoundRegistry.timer.singleShot = true;
			compoundRegistry.timer.timeout.connect(compoundRegistry.onTimer);
		}
		compoundRegistry.timer.start(800);
	} catch (e) {
		print("Compound registry: " + e);
	}
};

compoundRegistry.onTimer = function () {
	"use strict";
	var pending = compoundRegistry.pending, doc;
	compoundRegistry.pending = [];
	if (compoundRegistry.dialog) {
		return;
	}
	try {
		doc = mainWindow.activeDocument;
		if (doc && doc.isValid() && pending.indexOf(doc.uuid) !== -1 && !compoundRegistry.declined[doc.uuid]) {
			compoundRegistry.propose(false);
		}
	} catch (e) {
		print("Compound registry: " + e);
	}
};

// ---------------------------------------------------------------------------------------------------- report and PDF

//! The multiplet report of a 1D spectrum as MestReNova writes it (HTML, the user's journal format), or "".
compoundRegistry.multipletReport = function (aSpec) {
	"use strict";
	var reporter, wasHtml, report = "";
	try {
		if (aSpec.dimCount !== 1 || typeof MultipletReporter === "undefined") {
			return "";
		}
		reporter = MultipletReporter.getDefaultReporter();
		if (!reporter) {
			return "";
		}
		reporter.readSettings();
		wasHtml = reporter.useHtml;
		reporter.useHtml = true;
		try {
			report = reporter.report(aSpec, false);
		} finally {
			reporter.useHtml = wasHtml;
		}
	} catch (e) {
		print("Compound registry: multiplet report failed: " + e);
	}
	return report ? String(report) : "";
};

/** The peak list report of a 1D spectrum as MestReNova writes it ("13C NMR (126 MHz, CDCl3) d 171.1, 60.4."; HTML,
	the user's journal format and peak report options), or "" when it has no peak that would be reported. */
compoundRegistry.peakReport = function (aSpec) {
	"use strict";
	var reporter, peaks, peak, i, n = 0, report = "";
	try {
		if (aSpec.dimCount !== 1 || typeof PeakReporter === "undefined") {
			return "";
		}
		reporter = PeakReporter.getDefaultReporter();
		if (!reporter) {
			return "";
		}
		reporter.copySettings(PeakReporter.readSettings());
		peaks = new Peaks(aSpec.peaks());
		for (i = 0; i < peaks.count; i++) {
			peak = new Peak(peaks.at(i));
			if (aSpec.checkPeakVisibility(peak) && (!reporter.compoundOnly || peak.type === Peak.Compound)) {
				n++;
			}
		}
		if (n) {
			report = reporter.report(aSpec, false);
		}
	} catch (e) {
		print("Compound registry: peak report failed: " + e);
	}
	return report ? String(report) : "";
};

//! What is inside <body> of an HTML document.
compoundRegistry.htmlBody = function (aHtml) {
	"use strict";
	return String(aHtml).replace(/^[\s\S]*<body[^>]*>/i, "").replace(/<\/body>[\s\S]*$/i, "");
};

compoundRegistry.plainText = function (aHtml) {
	"use strict";
	var doc;
	if (!aHtml) {
		return "";
	}
	try {
		doc = new TextDocument();
		doc.setHtml(aHtml);
		return String(doc.toPlainText()).replace(/\s+/g, " ").replace(/^\s+|\s+$/g, "");
	} catch (e) {
		return String(aHtml).replace(/<[^>]*>/g, "").replace(/\s+/g, " ");
	}
};

//! Everything recorded about one spectrum: what it is, the multiplet report and the parameters table.
compoundRegistry.spectrumData = function (aSpec) {
	"use strict";
	var data = {parameters: {}}, names = [], i, value;
	function param(aName) {
		var v;
		try {
			v = aSpec.getParam(aName);
			return v === undefined || v === null ? "" : String(v);
		} catch (e) {
			return "";
		}
	}
	try { data.title = String(aSpec.title || ""); } catch (e1) { data.title = ""; }
	try { data.comment = String(aSpec.comment || ""); } catch (e2) { data.comment = ""; }
	try { data.dimensions = aSpec.dimCount; } catch (e3) { data.dimensions = 1; }
	try { data.nucleus = String(aSpec.nucleus()); } catch (e4) { data.nucleus = param("Nucleus"); }
	try { data.frequency_mhz = Number(aSpec.frequency()); } catch (e5) {}
	if (!(data.frequency_mhz > 0)) {
		delete data.frequency_mhz;
	}
	try { data.solvent = String(aSpec.solvent || ""); } catch (e6) { data.solvent = param("Solvent"); }
	data.experiment = param("Experiment");
	data.pulse_sequence = param("Pulse Sequence");
	try { names = aSpec.paramNames(); } catch (e7) {}
	for (i = 0; i < names.length; i++) {
		value = param(names[i]);
		if (value !== "") {
			data.parameters[names[i]] = value;
		}
	}
	data.parameterOrder = names;
	data.report_html = compoundRegistry.multipletReport(aSpec);
	data.report_kind = "multiplets";
	if (!data.report_html && data.dimensions === 1 && !/^1?H$/i.test(data.nucleus)) {
		data.report_html = compoundRegistry.peakReport(aSpec);
		data.report_kind = "peaks";
	}
	if (!data.report_html) {
		delete data.report_kind;
	}
	return data;
};

//! The page added to the PDF for one spectrum.
compoundRegistry.summaryHtml = function (aLink, aData) {
	"use strict";
	var h = compoundRegistry.html, rows = [], cells = [], i, name, half, lines = [];
	if (aLink.name) {
		lines.push(h(aLink.name));
	}
	lines.push((aLink.formula ? h(aLink.formula) + " &nbsp;&middot;&nbsp; " : "") + "Experiment " + h(aLink.experiment) +
		", product " + h(aLink.product_index) + (aLink.sample ? " &nbsp;&middot;&nbsp; Sample " + h(aLink.sample) : ""));
	// the PDF shows neither the title nor the comment of a spectrum (they stay in the document and in the registry)
	lines.push("<b>" + h(aData.nucleus || "") + "</b> " + h(aData.experiment || (aData.dimensions || 1) + "D") + (aData.solvent ? ", " + h(aData.solvent) : ""));
	for (i = 0; i < aData.parameterOrder.length; i++) {
		name = aData.parameterOrder[i];
		if (aData.parameters[name] !== undefined && !/^(title|comment)$/i.test(name)) {
			cells.push("<td><b>" + h(name) + "</b></td><td>" + h(aData.parameters[name]) + "</td>");
		}
	}
	half = Math.ceil(cells.length / 2);
	for (i = 0; i < half; i++) {
		rows.push("<tr>" + cells[i] + "<td>&nbsp;&nbsp;&nbsp;</td>" + (cells[i + half] || "<td></td><td></td>") + "</tr>");
	}
	return "<html><body style=\"font-family: Arial; font-size: 9pt\"><h2>" + h(aLink.compound_id) + "</h2><p>" + lines.join("<br/>") + "</p>" +
		"<p>" + (aData.report_html ? compoundRegistry.htmlBody(aData.report_html) : "<i>" + (aData.dimensions > 1 ? "2D spectrum: no shift report." :
			/^1?H$/i.test(aData.nucleus) ? "No multiplet report (no multiplets in this spectrum)." :
			"No shift report (no multiplets and no peaks in this spectrum).") + "</i>") + "</p><br/>" +
		"<table cellspacing=\"0\" cellpadding=\"2\">" + rows.join("") + "</table></body></html>";
};

//! Gives the file aFrom the name aTo (which must be free). Returns true when aTo exists afterwards.
compoundRegistry.moveFile = function (aFrom, aTo) {
	"use strict";
	try { File.rename(aFrom, aTo); } catch (e1) {}
	if (!File.exists(aTo)) {
		try { File.copy(aFrom, aTo); } catch (e2) {}
	}
	return File.exists(aTo);
};

//! True when aPath is a file with content (not a folder, not an empty file).
compoundRegistry.hasContent = function (aPath) {
	"use strict";
	try {
		return File.exists(aPath) && new File(aPath).size > 0;
	} catch (e) {
		return false;
	}
};

/** Exports the document to aPath: its pages as they look but without the title (title and comment) drawn on
	the spectra aItems, then one summary page per spectrum. Afterwards the titles are shown again and the summary
	pages removed. Returns true when the PDF was written. */
compoundRegistry.exportPdf = function (aDoc, aPath, aLink, aSpectra, aItems) {
	"use strict";
	var added = [], hidden = [], i, page, text, ok = false, current = null, margin = 6,
		// a name of its own for the new file: no document's PDF and no file of the user can have it
		fresh = String(aPath).replace(/\.pdf$/i, "") + "." + Application.createUuid().replace(/[\{\}]/g, "") + ".tmp.pdf";
	try { current = aDoc.curPage(); } catch (ignore) {}
	try {
		for (i = 0; aItems && i < aItems.length; i++) {
			try {
				if (aItems[i].getProperty("title.visible")) {
					aItems[i].setProperty("title.visible", false);
					aItems[i].update();
					hidden.push(aItems[i]);
				}
			} catch (e0) {
				print("Compound registry: could not hide a spectrum title: " + e0);
			}
		}
		for (i = 0; i < aSpectra.length; i++) {
			aDoc.newPage(true);
			page = aDoc.page(aDoc.pageCount() - 1);
			added.push(page);
			aDoc.setCurPage(page);
			text = draw.text({text: compoundRegistry.summaryHtml(aLink, aSpectra[i]), isHtml: true, autoSize: false, autoHeight: false});
			text.left = page.left + margin;
			text.top = page.top + margin;
			text.width = page.width - 2 * margin;
			text.height = page.height - 2 * margin;
			text.update();
		}
		// Written under another name first: an export that fails leaves the earlier PDF as it is.
		// exportToPDF returns true also when it wrote nothing, so only the file itself tells.
		aDoc.exportToPDF(fresh);
		if (compoundRegistry.hasContent(fresh)) {
			if (File.exists(aPath)) {
				// an earlier PDF that is open in a viewer cannot be removed: it is kept
				try { File.remove(aPath); } catch (e0) {}
			}
			if (!File.exists(aPath)) {
				ok = compoundRegistry.moveFile(fresh, aPath) && compoundRegistry.hasContent(aPath);
				if (!ok) {
					// the new file could not be given its name: write it in place
					aDoc.exportToPDF(aPath);
					ok = compoundRegistry.hasContent(aPath);
				}
			}
		}
	} catch (e) {
		print("Compound registry: PDF export failed: " + e);
	} finally {
		try {
			if (File.exists(fresh)) {
				File.remove(fresh);
			}
		} catch (e2) {
			print("Compound registry: could not remove " + fresh + ": " + e2);
		}
		for (i = 0; i < hidden.length; i++) {
			try {
				hidden[i].setProperty("title.visible", true);
				hidden[i].update();
			} catch (e1) {
				print("Compound registry: could not show a spectrum title again: " + e1);
			}
		}
		try {
			if (added.length) {
				aDoc.deletePages(added);
			}
			if (current && current.isValid()) {
				aDoc.setCurPage(current);
			}
		} catch (e2) {
			print("Compound registry: could not remove the summary page: " + e2);
		}
	}
	return ok;
};

/** Saves the linked active document, its PDF and the NMR data in the registry.
	Returns {mnova, pdf, recorded, reports: [...], problems: [...]} or null when nothing was saved. */
compoundRegistry.publish = function (aDoc, aLink) {
	"use strict";
	var specs = compoundRegistry.spectra(aDoc), data = [], i, path, pdf, folder, target, att, meta = {}, key, answer,
		out = {mnova: "", pdf: "", recorded: false, reports: [], problems: []}, payload = [], item, keys = {};
	for (i = 0; i < specs.length; i++) {
		item = compoundRegistry.spectrumData(specs[i]);
		// the name the registry files a spectrum under; two spectra of one dataset in a document get "#2", "#3"
		key = (String(item.title).split("\n")[0].replace(/^\s+|\s+$/g, "") || "untitled") + " | " + (item.nucleus || "?") + " | " + (item.dimensions || 1) + "D";
		keys[key] = (keys[key] || 0) + 1;
		item.key = (keys[key] > 1 ? key + " #" + keys[key] : key).substring(0, 300);
		data.push(item);
	}
	folder = compoundRegistry.compoundFolder(aLink.core_stereo_id);
	if (!folder) {
		return null;
	}
	// a document that already lies in the compound's folder keeps its name; any other is saved into the folder
	path = compoundRegistry.slashes(aDoc.name);
	if (!/\.mnova$/i.test(path) || !File.exists(path) ||
			path.substring(0, path.lastIndexOf("/")).toLowerCase() !== folder.toLowerCase()) {
		target = compoundRegistry.targetPath(aDoc, folder, {core_stereo_id: aLink.core_stereo_id, full_id: aLink.compound_id,
			match: {file_label: aLink.file_label || String(aLink.sample || "").replace(/[^A-Za-z0-9.+\-]+/g, "_").substring(0, 60)}});
		path = target.path;
	}
	pdf = path.replace(/\.mnova$/i, "") + ".pdf";

	if (compoundRegistry.exportPdf(aDoc, pdf, aLink, data, specs)) {
		out.pdf = pdf;
	} else if (File.exists(pdf)) {
		// the earlier PDF is still there and stays the one recorded for the spectra
		out.pdf = pdf;
		out.problems.push("The PDF could not be written again, the earlier one was kept (is " + pdf + " open in another program?).");
	} else {
		out.problems.push("The PDF could not be written (is " + pdf + " open in another program?).");
	}

	for (key in aLink) {
		if (aLink.hasOwnProperty(key)) {
			meta[key] = aLink[key];
		}
	}
	meta.nmr = [];
	for (i = 0; i < data.length; i++) {
		item = {title: data[i].title, nucleus: data[i].nucleus, dimensions: data[i].dimensions, kind: data[i].report_kind || "",
			report: compoundRegistry.plainText(data[i].report_html)};
		meta.nmr.push(item);
		out.reports.push(item);
	}
	meta.saved_at = new Date().toISOString();
	try {
		att = aDoc.getAttachment(compoundRegistry.attachmentName) || new Attachment();
		att.setMetaData(meta);
		aDoc.setAttachment(compoundRegistry.attachmentName, att);
	} catch (e) {
		out.problems.push("The report could not be stored inside the document: " + e);
	}
	serialization.save(path, "mnova");
	if (File.exists(path)) {
		out.mnova = path;
	} else {
		out.problems.push("The document could not be saved as " + path + ".");
	}

	for (i = 0; i < data.length; i++) {
		item = {key: data[i].key, title: data[i].title, comment: data[i].comment, sample: aLink.sample || "", nucleus: data[i].nucleus,
			dimensions: data[i].dimensions, solvent: data[i].solvent, experiment: data[i].experiment,
			pulse_sequence: data[i].pulse_sequence, report_html: data[i].report_html, report_kind: data[i].report_kind || "", parameters: data[i].parameters,
			mnova_file: out.mnova, pdf_file: out.pdf};
		if (data[i].frequency_mhz) {
			item.frequency_mhz = data[i].frequency_mhz;
		}
		payload.push(item);
	}
	answer = compoundRegistry.runBridge("record", compoundRegistry.jsonAscii({full_id: aLink.compound_id, spectra: payload}),
		"Saving the NMR data with the compound...");
	if (answer.ok) {
		out.recorded = true;
	} else {
		out.problems.push("The registry did not store the NMR data: " + (answer.error && answer.error.message ? answer.error.message : "unknown error"));
	}
	return out;
};

//! Menu action: save the document, the PDF and the multiplet report with the compound (links first when needed).
compoundRegistry.saveToCompound = function () {
	"use strict";
	var doc = mainWindow.activeDocument, link, result, i, r, text, missing = [], others = 0;
	if (!doc || !doc.isValid()) {
		MessageBox.information("Open a spectrum first.");
		return null;
	}
	link = compoundRegistry.linkOf(doc);
	if (!link || !link.compound_id) {
		if (!compoundRegistry.propose(true)) {
			return null;
		}
		doc = mainWindow.activeDocument;
		link = compoundRegistry.linkOf(doc);
		if (!link || !link.compound_id) {
			return null;
		}
	}
	result = compoundRegistry.publish(doc, link);
	if (!result) {
		return null;
	}
	text = "Saved with " + link.compound_id + ":";
	for (i = 0; i < result.reports.length; i++) {
		r = result.reports[i];
		if (r.report) {
			text += "\n\n" + r.report;
		} else if (r.dimensions > 1) {
			others++;
		} else {
			missing.push(r.nucleus + " (" + String(r.title).split("\n")[0] + "): " +
				(/^1?H$/i.test(r.nucleus) ? "no multiplets; run the multiplet analysis" : "no multiplets and no peaks; pick the peaks or run the multiplet analysis"));
		}
	}
	if (missing.length) {
		text += "\n\nNo shift report was saved for:\n- " + missing.join("\n- ") + "\nThen save again.";
	}
	if (others) {
		text += "\n\n" + others + " 2D " + (others === 1 ? "spectrum is" : "spectra are") + " in the PDF and recorded with " +
			(others === 1 ? "its" : "their") + " parameters (no shift report).";
	}
	text += "\n\n" + (result.mnova ? result.mnova + "\n" : "") + (result.pdf || "");
	if (result.problems.length) {
		MessageBox.warning(text + "\n\nProblems:\n- " + result.problems.join("\n- "));
	} else if (mainWindow.hasGui) {
		MessageBox.information(text);
	}
	try { mainWindow.setStatusMessage("Saved report and PDF with " + link.compound_id); } catch (ignore) {}
	return result;
};

// ---------------------------------------------------------------------------------------------------- structure

/** Puts aMolfile on the current page of aDoc, in the upper left corner at about a quarter of the page (the
	user moves and resizes it like any item). Returns the new item or null. */
compoundRegistry.placeStructure = function (aDoc, aMolfile) {
	"use strict";
	var id, item, page, scale, margin = 8;
	id = molecule.importMolfile(aMolfile);
	item = id ? new Molecule(aDoc.getItem(id)) : null;
	if (!item || !item.isValid()) {
		return null;
	}
	try {
		page = aDoc.curPage();
		if (item.width > 0 && item.height > 0) {
			scale = Math.min(page.width * 0.28 / item.width, page.height * 0.3 / item.height);
			item.width = item.width * scale;
			item.height = item.height * scale;
		}
		item.left = page.left + margin;
		item.top = page.top + margin;
		item.update();
		aDoc.update();
	} catch (e) {
		print("Compound registry: the structure was added but could not be placed: " + e);
	}
	return item;
};

//! Menu action: paste the structure of the linked compound on the current page (links first when needed).
compoundRegistry.pasteStructure = function () {
	"use strict";
	var doc = mainWindow.activeDocument, link, info, item;
	if (!doc || !doc.isValid()) {
		MessageBox.information("Open a spectrum first.");
		return null;
	}
	link = compoundRegistry.linkOf(doc);
	if (!link || !link.compound_id) {
		if (!compoundRegistry.propose(true)) {
			return null;
		}
		doc = mainWindow.activeDocument;
		link = compoundRegistry.linkOf(doc);
		if (!link || !link.compound_id) {
			return null;
		}
	}
	if (mainWindow.hasGui && doc.itemCount("Molecule") > 0 &&
			MessageBox.question("This document already contains a structure.\nAdd the structure of " + link.compound_id + " as well?",
				MessageBox.Yes, MessageBox.No) !== MessageBox.Yes) {
		return null;
	}
	info = compoundRegistry.compoundInfo(link.core_stereo_id);
	if (!info) {
		return null;
	}
	if (!info.molfile) {
		MessageBox.warning("The registry has no structure drawing for " + link.core_stereo_id + ".");
		return null;
	}
	item = compoundRegistry.placeStructure(doc, info.molfile);
	if (!item) {
		MessageBox.critical("MestReNova could not add the structure of " + link.compound_id + " to the page.");
		return null;
	}
	try { mainWindow.setStatusMessage("Structure of " + link.compound_id + " added. It is saved with the document the next time you save."); } catch (ignore) {}
	return item;
};

// ---------------------------------------------------------------------------------------------------- library

//! Shows a dialog and waits until it is closed.
compoundRegistry.execDialog = function (aUi) {
	"use strict";
	return aUi.exec();
};

//! Text for the attribute or the content of the dialog XML.
compoundRegistry.xml = function (aText) {
	"use strict";
	return String(aText).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
};

//! The library dialog: search field, a scrollable column with one row (button) per compound.
compoundRegistry.libraryUi = function (aCount) {
	"use strict";
	var i, rows = "";
	for (i = 0; i < aCount; i++) {
		rows += '<item><widget class="QToolButton" name="row' + i + '">' +
			'<property name="toolButtonStyle"><enum>Qt::ToolButtonTextBesideIcon</enum></property>' +
			'<property name="iconSize"><size><width>165</width><height>112</height></size></property>' +
			'<property name="sizePolicy"><sizepolicy hsizetype="Expanding" vsizetype="Fixed"><horstretch>0</horstretch><verstretch>0</verstretch></sizepolicy></property>' +
			'<property name="minimumSize"><size><width>0</width><height>120</height></size></property>' +
			'<property name="autoRaise"><bool>false</bool></property></widget></item>';
	}
	return '<?xml version="1.0" encoding="UTF-8"?><ui version="4.0"><class>Dialog</class><widget class="QDialog" name="Dialog">' +
		'<property name="geometry"><rect><x>0</x><y>0</y><width>640</width><height>720</height></rect></property>' +
		'<property name="windowTitle"><string>Compound library</string></property>' +
		'<layout class="QVBoxLayout" name="mainLayout">' +
		'<item><layout class="QHBoxLayout" name="searchLayout">' +
		'<item><widget class="QLabel" name="lblSearch"><property name="text"><string>Search:</string></property></widget></item>' +
		'<item><widget class="QLineEdit" name="leSearch"><property name="placeholderText"><string>compound ID, experiment number (e.g. ABC-006), name or formula</string></property></widget></item>' +
		'<item><widget class="QPushButton" name="pbSearch"><property name="text"><string>Search</string></property><property name="default"><bool>true</bool></property></widget></item>' +
		'</layout></item>' +
		'<item><widget class="QCheckBox" name="chkNmr"><property name="text"><string>Only compounds with a saved NMR document</string></property></widget></item>' +
		'<item><widget class="QLabel" name="lblInfo"><property name="wordWrap"><bool>true</bool></property><property name="textFormat"><enum>Qt::RichText</enum></property></widget></item>' +
		'<item><widget class="QScrollArea" name="scroll"><property name="widgetResizable"><bool>true</bool></property>' +
		'<property name="horizontalScrollBarPolicy"><enum>Qt::ScrollBarAlwaysOff</enum></property>' +
		'<widget class="QWidget" name="inner"><layout class="QVBoxLayout" name="innerLayout">' + rows +
		'<item><spacer name="vs"><property name="orientation"><enum>Qt::Vertical</enum></property></spacer></item>' +
		'</layout></widget></widget></item>' +
		'<item><layout class="QHBoxLayout" name="bottomLayout">' +
		'<item><spacer name="sp"><property name="orientation"><enum>Qt::Horizontal</enum></property></spacer></item>' +
		'<item><widget class="QPushButton" name="pbCancel"><property name="text"><string>Close</string></property><property name="autoDefault"><bool>false</bool></property></widget></item>' +
		'</layout></item>' +
		'</layout></widget><resources/><connections/></ui>';
};

//! The words of a typed search, as the registry compares them (letters and digits only, lower case).
compoundRegistry.searchWords = function (aText) {
	"use strict";
	var parts = String(aText || "").toLowerCase().split(/\s+/), i, words = [], word;
	for (i = 0; i < parts.length; i++) {
		word = parts[i].replace(/[^a-z0-9]+/g, "");
		if (word) {
			words.push(word);
		}
	}
	return words;
};

//! Whether a compound of the library fits the typed words (same rule as the registry's own search).
compoundRegistry.libraryFits = function (aCompound, aWords) {
	"use strict";
	var hay = [aCompound.core_stereo_id, aCompound.name || "", aCompound.formula || ""].concat(aCompound.ids, aCompound.experiments), i, j, found;
	for (i = 0; i < aWords.length; i++) {
		found = false;
		for (j = 0; j < hay.length && !found; j++) {
			found = String(hay[j]).toLowerCase().replace(/[^a-z0-9]+/g, "").indexOf(aWords[i]) !== -1;
		}
		if (!found) {
			return false;
		}
	}
	return true;
};

//! The text of a library row.
compoundRegistry.libraryLabel = function (aCompound) {
	"use strict";
	var c = aCompound, n = c.documents.length, name = c.name || "", label;
	label = c.core_stereo_id + (c.formula ? "   " + c.formula : "");
	if (name) {
		label += "\n" + (name.length > 60 ? name.substring(0, 58) + "..." : name);
	}
	label += "\n" + c.experiments.slice(0, 4).join(", ") + (c.experiments.length > 4 ? ", ..." : "");
	label += "\n" + (n ? n + (n === 1 ? " NMR document" : " NMR documents") : "no NMR document saved yet");
	return label;
};

/** Shows the library. Returns the chosen compound, {search: text} when the registry has to be asked again
	(only when it has more compounds than one page shows), or null. */
compoundRegistry.chooseFromLibrary = function (aResult, aSearchText) {
	"use strict";
	var list = aResult.compounds, ui = Application.loadUi(compoundRegistry.libraryUi(list.length)), w = ui.widgets,
		i, c, chosen = null;

	function pick(aIndex) {
		return function () {
			chosen = list[aIndex];
			ui.accept();
		};
	}
	function filter() {
		var words = compoundRegistry.searchWords(w.leSearch.text), only = w.chkNmr.checked, shown = 0, j, visible;
		for (j = 0; j < list.length; j++) {
			visible = (!only || list[j].documents.length > 0) && compoundRegistry.libraryFits(list[j], words);
			w["row" + j].visible = visible;
			if (visible) {
				shown++;
			}
		}
		w.lblInfo.text = shown + " of " + list.length + (list.length === 1 ? " compound" : " compounds") + " shown." +
			(aResult.truncated ? " The registry has <b>" + aResult.total + "</b> that fit" + (aResult.query ? " <b>" + compoundRegistry.html(aResult.query) + "</b>" : "") +
				"; type more and press Search to look through all of them." : "") +
			(shown ? " Click a compound to open its NMR document." : (list.length ? " Nothing fits; change the search or untick the box." : " The registry has no compound yet."));
		return shown;
	}
	for (i = 0; i < list.length; i++) {
		c = list[i];
		w["row" + i].text = compoundRegistry.libraryLabel(c);
		w["row" + i].toolTip = c.core_stereo_id + (c.name ? "\n" + c.name : "") + "\n" + c.ids.join(", ") + "\n" + c.smiles;
		if (c.image) {
			w["row" + i].setIcon(c.image, 165, 112);
		}
		w["row" + i].clicked.connect(pick(i));
	}
	w.chkNmr.checked = String(settings.value(compoundRegistry.libraryKey, "true")) !== "false";
	w.leSearch.text = aSearchText || "";
	filter();
	w.leSearch.textChanged.connect(filter);
	w.chkNmr.toggled.connect(filter);
	w.pbSearch.clicked.connect(function () {
		// everything is in the list already unless the registry cut it; then it is asked again
		if (aResult.truncated || aResult.query) {
			chosen = {search: String(w.leSearch.text).replace(/^\s+|\s+$/g, "")};
			ui.accept();
		} else {
			filter();
		}
	});
	w.pbCancel.clicked.connect(function () { ui.reject(); });
	try { w.leSearch.setFocus(); } catch (ignore) {}

	compoundRegistry.dialog = ui;
	try {
		compoundRegistry.execDialog(ui);
	} finally {
		compoundRegistry.dialog = null;
	}
	settings.setValue(compoundRegistry.libraryKey, w.chkNmr.checked ? "true" : "false");
	return chosen;
};

//! Asks which of several documents of a compound to open. Returns the document or null.
compoundRegistry.chooseDocument = function (aCompound) {
	"use strict";
	var docs = aCompound.documents, ui, w, i, chosen = null;
	if (docs.length === 1) {
		return docs[0];
	}
	ui = Application.loadUi('<?xml version="1.0" encoding="UTF-8"?><ui version="4.0"><class>Dialog</class><widget class="QDialog" name="Dialog">' +
		'<property name="windowTitle"><string>NMR documents of ' + compoundRegistry.xml(aCompound.core_stereo_id) + '</string></property>' +
		'<layout class="QVBoxLayout" name="mainLayout">' +
		'<item><widget class="QLabel" name="lblIntro"><property name="text"><string>' + docs.length + ' documents are saved for this compound (newest first). Which one?</string></property></widget></item>' +
		'<item><widget class="QComboBox" name="cbDoc"><property name="minimumSize"><size><width>460</width><height>0</height></size></property></widget></item>' +
		'<item><layout class="QHBoxLayout" name="bottomLayout">' +
		'<item><spacer name="sp"><property name="orientation"><enum>Qt::Horizontal</enum></property></spacer></item>' +
		'<item><widget class="QPushButton" name="pbOpen"><property name="text"><string>Open</string></property><property name="default"><bool>true</bool></property></widget></item>' +
		'<item><widget class="QPushButton" name="pbCancel"><property name="text"><string>Back</string></property></widget></item>' +
		'</layout></item>' +
		'</layout></widget><resources/><connections/></ui>');
	w = ui.widgets;
	for (i = 0; i < docs.length; i++) {
		w.cbDoc.addItem(docs[i].name + "   (" + String(docs[i].modified).substring(0, 10) + ")");
	}
	w.pbOpen.clicked.connect(function () {
		chosen = docs[w.cbDoc.currentIndex];
		ui.accept();
	});
	w.pbCancel.clicked.connect(function () { ui.reject(); });
	compoundRegistry.dialog = ui;
	try {
		compoundRegistry.execDialog(ui);
	} finally {
		compoundRegistry.dialog = null;
	}
	return chosen;
};

/** Opens a saved document of the library as a document of its own (the user's spectrum is not touched).
	A document that is open already is not opened twice. Returns the path or "". */
compoundRegistry.openReference = function (aPath) {
	"use strict";
	var path = compoundRegistry.slashes(aPath), docs, i;
	if (!File.exists(path)) {
		MessageBox.warning("The document is not there any more:\n" + path);
		return "";
	}
	try {
		docs = mainWindow.documents();
		for (i = 0; i < docs.length; i++) {
			if (compoundRegistry.slashes(docs[i].name).toLowerCase() === path.toLowerCase()) {
				try { mainWindow.setStatusMessage(path + " is open already."); } catch (ignore) {}
				return path;
			}
		}
	} catch (e) {
		print("Compound registry: " + e);
	}
	if (!serialization.open(path)) {
		MessageBox.critical("MestReNova could not open\n" + path);
		return "";
	}
	try { mainWindow.setStatusMessage("Opened " + path + " for comparison."); } catch (ignore2) {}
	return path;
};

//! Menu action: the compound library. Returns the path of the opened document or "".
compoundRegistry.library = function () {
	"use strict";
	var answer, chosen, doc, typed = "", query = "";
	while (true) {
		answer = compoundRegistry.runBridge("library", compoundRegistry.jsonAscii({query: query}), "Reading the compound library...");
		if (!answer.ok) {
			MessageBox.critical("Compound registry: " + (answer.error && answer.error.message ? answer.error.message : "unknown error"));
			return "";
		}
		while (true) {
			chosen = compoundRegistry.chooseFromLibrary(answer.result, typed);
			if (!chosen) {
				return "";
			}
			if (chosen.search !== undefined) {
				typed = chosen.search;
				query = chosen.search;
				break;
			}
			if (!chosen.documents.length) {
				MessageBox.information("No NMR document is saved for " + chosen.core_stereo_id + " yet.\n" +
					"(Its folder: " + (chosen.folder || "none") + ")");
			} else {
				doc = compoundRegistry.chooseDocument(chosen);
				if (doc) {
					return compoundRegistry.openReference(doc.path);
				}
			}
		}
	}
};

compoundRegistry.showLink = function () {
	"use strict";
	var doc = mainWindow.activeDocument, link = doc && doc.isValid() ? compoundRegistry.linkOf(doc) : null;
	if (!link || !link.compound_id) {
		MessageBox.information("This document is not linked to a compound ID.");
		return;
	}
	MessageBox.information("Compound ID: " + link.compound_id +
		(link.name ? "\nName: " + link.name : "") +
		(link.formula ? "\nFormula: " + link.formula : "") +
		"\nExperiment: " + link.experiment + ", product " + link.product_index +
		"\nSample: " + link.sample +
		"\nLinked: " + link.linked_at +
		"\nFile: " + doc.name);
};

// ---------------------------------------------------------------------------------------------------- user interface
if (typeof MnUi !== "undefined" && MnUi.compoundRegistry) {
	MnUi.compoundRegistry.compoundRegistryLink = function () {
		"use strict";
		compoundRegistry.propose(true);
	};
	MnUi.compoundRegistry.compoundRegistrySave = function () {
		"use strict";
		compoundRegistry.saveToCompound();
	};
	MnUi.compoundRegistry.compoundRegistryStructure = function () {
		"use strict";
		compoundRegistry.pasteStructure();
	};
	MnUi.compoundRegistry.compoundRegistryLibrary = function () {
		"use strict";
		compoundRegistry.library();
	};
	MnUi.compoundRegistry.compoundRegistryShow = function () {
		"use strict";
		compoundRegistry.showLink();
	};
	MnUi.compoundRegistry.compoundRegistryAuto = function () {
		"use strict";
		settings.setValue(compoundRegistry.autoKey, compoundRegistry.autoPropose() ? "false" : "true");
	};
	MnUi.compoundRegistry.update = function (aUpdateObj) {
		"use strict";
		if (aUpdateObj.compoundRegistryAuto) {
			aUpdateObj.compoundRegistryAuto.checked = compoundRegistry.autoPropose();
		}
	};
}

if (Application.engine === "cpp") {
	Application.mainWindow.installEventHandler("nmrSpectrumImported", "compoundRegistry.onSpectrumImported");
}
