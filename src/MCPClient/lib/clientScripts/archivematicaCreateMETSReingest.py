#!/usr/bin/env python2
from __future__ import print_function
from lxml import etree
import os
import sys

import metsrw

import archivematicaCreateMETS2 as createmets2
import archivematicaCreateMETSRights as createmetsrights
import archivematicaCreateMETSMetadataCSV as createmetscsv
import namespaces as ns

# dashboard
from main import models


def update_dublincore(mets, sip_uuid):
    """
    Add new dmdSec for updated Dublin Core info relating to entire SIP.

    Case: No DC in DB, no DC in METS: Do nothing
    Case: No DC in DB, DC in METS: Mark as deleted.
    Case: DC in DB is untouched (METADATA_STATUS_REINGEST): Do nothing
    Case: New DC in DB with METADATA_STATUS_ORIGINAL: Add new DC
    Case: DC in DB with METADATA_STATUS_UPDATED: mark old, create updated
    """

    # Check for DC in DB with METADATA_STATUS_UPDATED or METADATA_STATUS_ORIGINAL
    untouched = models.DublinCore.objects.filter(
        metadataappliestoidentifier=sip_uuid,
        metadataappliestotype_id=createmets2.SIPMetadataAppliesToType,
        status=models.METADATA_STATUS_REINGEST).exists()
    if untouched:
        # No new or updated DC found - return early
        print('No updated or new DC metadata found')
        return mets

    # Get structMap element related to SIP DC info
    objects_div = mets.get_file(label='objects', type='Directory')
    print('Existing dmdIds for DC metadata:', objects_div.dmdids)

    # Create element
    dc_elem = createmets2.getDublinCore(createmets2.SIPMetadataAppliesToType, sip_uuid)

    if dc_elem is None:
        if objects_div.dmdsecs:
            print('DC metadata was deleted')
            # Create 'deleted' DC element
            dc_elem = etree.Element(ns.dctermsBNS + "dublincore", nsmap={"dcterms": ns.dctermsNS, 'dc': ns.dcNS})
            dc_elem.set(ns.xsiBNS + "schemaLocation", ns.dctermsNS + " http://dublincore.org/schemas/xmls/qdc/2008/02/11/dcterms.xsd")
        else:
            # No new or updated DC found - return early
            print('No updated or new DC metadata found')
            return mets
    dmdsec = objects_div.add_dublin_core(dc_elem)
    print('Adding new DC in dmdSec with ID', dmdsec.id_string())
    if len(objects_div.dmdsecs) > 1:
        objects_div.dmdsecs[-2].replace_with(dmdsec)

    return mets


def update_rights(mets, sip_uuid):
    """
    Add rightsMDs for updated PREMIS Rights.
    """
    # Get original files to add rights to
    original_files = [f for f in mets.all_files() if f.use == 'original']

    # Check for newly added rights
    rights_list = models.RightsStatement.objects.filter(
        metadataappliestoidentifier=sip_uuid,
        metadataappliestotype_id=createmets2.SIPMetadataAppliesToType,
        status=models.METADATA_STATUS_ORIGINAL
    )
    if not rights_list:
        print('No new rights added')
    else:
        add_rights_elements(rights_list, original_files)

    # Check for updated rights
    rights_list = models.RightsStatement.objects.filter(
        metadataappliestoidentifier=sip_uuid,
        metadataappliestotype_id=createmets2.SIPMetadataAppliesToType,
        status=models.METADATA_STATUS_UPDATED
    )
    if not rights_list:
        print('No updated rights found')
    else:
        add_rights_elements(rights_list, original_files, updated=True)

    return mets


def add_rights_elements(rights_list, files, updated=False):
    """
    Create and add rightsMDs for everything in rights_list to files.
    """
    # Add to files' amdSecs
    for fsentry in files:
        for rights in rights_list:
            # Create element
            new_rightsmd = fsentry.add_premis_rights(createmetsrights.createRightsStatement(rights, fsentry.file_uuid))
            print('Adding rightsMD', new_rightsmd.id_string(), 'to amdSec with ID', fsentry.amdsecs[0].id_string(), 'for file', fsentry.file_uuid)

            if updated:
                # Mark as replacing another rightsMD
                # rightsBasis is semantically unique (though not currently enforced in code). Assume that this replaces a rightsMD with the same basis
                # Find the most ce
                superseded = [s for s in fsentry.amdsecs[0].subsections if s.subsection == 'rightsMD']
                superseded = sorted(superseded, key=lambda x: x.created)
                # NOTE sort(..., reverse=True) behaves differently with unsortable elements like '' and None
                for s in superseded[::-1]:
                    print('created', s.created)
                    if s.serialize().xpath('.//premis:rightsBasis[text()="' + rights.rightsbasis + '"]', namespaces=ns.NSMAP):
                        s.replace_with(new_rightsmd)
                        print('rightsMD', new_rightsmd.id_string(), 'replaces rightsMD', s.id_string())
                        break


def add_events(mets, sip_uuid):
    """
    Add reingest events for all existing files.
    """
    # Get all reingestion events for files in this SIP
    reingest_events = models.Event.objects.filter(file_uuid__sip__uuid=sip_uuid, event_type='reingestion')

    # Get Agent
    try:
        agent = models.Agent.objects.get(identifiertype="preservation system", name="Archivematica", agenttype="software")
    except models.Agent.DoesNotExist:
        agent = None
    except models.Agent.MultipleObjectsReturned:
        agent = None
        print('WARNING multiple agents found for Archivematica')

    for event in reingest_events:
        print('Adding reingestion event to', event.file_uuid_id)
        fsentry = mets.get_file(file_uuid=event.file_uuid_id)
        fsentry.add_premis_event(createmets2.createEvent(event))

        amdsec = fsentry.amdsecs[0]

        # Add agent if it's not already in this amdSec
        if agent and not mets.tree.xpath('mets:amdSec[@AMDID="' + amdsec.id_string() + '"]//mets:mdWrap[@MDTYPE="PREMIS:AGENT"]//premis:agentIdentifierValue[text()="' + agent.identifiervalue + '"]', namespaces=ns.NSMAP):
            print('Adding Agent for', agent.identifiervalue)
            fsentry.add_premis_agent(createmets2.createAgent(
                agent.identifiertype, agent.identifiervalue,
                agent.name, agent.agenttype))

    return mets


def add_new_files(mets, sip_uuid, sip_dir):
    """
    Add new metadata files to structMap, fileSec. Parse metadata.csv to dmdSecs.
    """
    # Find new metadata files
    # How tell new file from old with same name? Check hash?
    # QUESTION should the metadata.csv be parsed and only updated if different even if one already existed?
    new_files = []
    metadata_path = os.path.join(sip_dir, 'objects', 'metadata')
    metadata_csv = None
    for dirpath, _, filenames in os.walk(metadata_path):
        for filename in filenames:
            # Find in METS
            current_loc = os.path.join(dirpath, filename).replace(sip_dir, '%SIPDirectory%', 1)
            rel_path = current_loc.replace('%SIPDirectory%', '', 1)
            print('Looking for', rel_path, 'in METS')
            fsentry = mets.get_file(path=rel_path)
            if fsentry is None:
                # If not in METS, get File object and store for later
                print(rel_path, 'not found in METS, must be new file')
                f = models.File.objects.get(
                    sip_id=sip_uuid,
                    filegrpuse='metadata',
                    currentlocation=current_loc,
                )
                new_files.append(f)
                if rel_path == 'objects/metadata/metadata.csv':
                    metadata_csv = f
            else:
                print(rel_path, 'found in METS, no further work needed')

    if not new_files:
        return mets

    # Set global counters so getAMDSec will work
    createmets2.globalAmdSecCounter = int(mets.tree.xpath('count(mets:amdSec)', namespaces=ns.NSMAP))
    createmets2.globalTechMDCounter = int(mets.tree.xpath('count(mets:amdSec/mets:techMD)', namespaces=ns.NSMAP))
    createmets2.globalDigiprovMDCounter = int(mets.tree.xpath('count(mets:amdSec/mets:digiprovMD)', namespaces=ns.NSMAP))

    metadata_fsentry = mets.get_file(label='metadata', type='Directory')

    for f in new_files:
        # Create amdSecs
        print('Adding amdSec for', f.currentlocation, '(', f.uuid, ')')
        amdsec, amdid = createmets2.getAMDSec(
            fileUUID=f.uuid,
            filePath=None,  # Only needed if use=original
            use='metadata',
            type=None,  # Not used
            id=None,  # Not used
            transferUUID=None,  # Only needed if use=original
            itemdirectoryPath=None,  # Only needed if use=original
            typeOfTransfer=None,  # Only needed if use=original
            baseDirectoryPath=sip_dir,
        )
        print(f.uuid, 'has amdSec with ID', amdid)

        # Create parent directories if needed
        dirs = os.path.dirname(f.currentlocation.replace('%SIPDirectory%objects/metadata/', '', 1)).split('/')
        parent_fsentry = metadata_fsentry
        for dirname in (d for d in dirs if d):
            child = metsrw.FSEntry(
                path=None,
                type='Directory',
                label=dirname,
            )
            parent_fsentry.add_child(child)
            parent_fsentry = child

        entry = metsrw.FSEntry(
            path=f.currentlocation.replace('%SIPDirectory%', '', 1),
            use='metadata',
            type='Item',
            file_uuid=f.uuid,
        )
        metsrw_amdsec = metsrw.AMDSec(tree=amdsec, section_id=amdid)
        entry.amdsecs.append(metsrw_amdsec)
        parent_fsentry.add_child(entry)

    # Parse metadata.csv and add dmdSecs
    if metadata_csv:
        mets = update_metadata_csv(mets, metadata_csv, sip_uuid, sip_dir)

    return mets


def delete_files(mets, sip_uuid):
    """
    Update METS for deleted files.

    Add a deletion event, update fileGrp USE to deleted, and remove FLocat.
    """
    deleted_files = models.File.objects.filter(
        sip_id=sip_uuid,
        event__event_type='deletion',
    ).values_list('uuid', flat=True)
    for file_uuid in deleted_files:
        df = mets.get_file(file_uuid=file_uuid)
        df.use = 'deleted'
        df.path = None
        df.label = None
    return mets


def update_metadata_csv(mets, metadata_csv, sip_uuid, sip_dir):
    print('Parse new metadata.csv')
    full_path = metadata_csv.currentlocation.replace('%SIPDirectory%', sip_dir, 1)
    csvmetadata = createmetscsv.parseMetadataCSV(full_path)

    # FIXME This doesn't support having both DC and non-DC metadata in dmdSecs
    # If createDmdSecsFromCSVParsedMetadata returns more than 1 dmdSec, behaviour is undefined
    for f, md in csvmetadata.iteritems():
        # Verify file is in AIP
        print('Looking for', f, 'from metadata.csv in SIP')
        # Find File with original or current locationg matching metadata.csv
        # Prepend % to match the end of %SIPDirectory% or %transferDirectory%
        try:
            file_obj = models.File.objects.get(sip_id=sip_uuid, originallocation__endswith='%' + f)
        except models.File.DoesNotExist:
            try:
                file_obj = models.File.objects.get(sip_id=sip_uuid, currentlocation__endswith='%' + f)
            except models.File.DoesNotExist:
                print(f, 'not found in database')
                continue
        print(f, 'found in database')

        fsentry = mets.get_file(file_uuid=file_obj.uuid)
        print(f, 'was associated with', fsentry.dmdids)

        # Create dmdSec
        new_dmdsecs = createmets2.createDmdSecsFromCSVParsedMetadata(md)
        # Add both
        for new_dmdsec in new_dmdsecs:
            # need to strip new_d to just the DC part
            new_dc = new_dmdsec.find('.//dcterms:dublincore', namespaces=ns.NSMAP)
            new_metsrw_dmdsec = fsentry.add_dublin_core(new_dc)
            if len(fsentry.dmdsecs) > 1:
                fsentry.dmdsecs[-2].replace_with(new_metsrw_dmdsec)

        print(f, 'now associated with', fsentry.dmdids)

    return mets


def update_mets(sip_dir, sip_uuid):
    old_mets_path = os.path.join(
        sip_dir,
        'objects',
        'submissionDocumentation',
        'METS.' + sip_uuid + '.xml')
    print('Looking for old METS at path', old_mets_path)

    # Parse old METS
    mets = metsrw.METSDocument.fromfile(old_mets_path)

    update_dublincore(mets, sip_uuid)
    update_rights(mets, sip_uuid)
    add_events(mets, sip_uuid)
    add_new_files(mets, sip_uuid, sip_dir)
    delete_files(mets, sip_uuid)

    # Delete original METS

    return mets.serialize()

if __name__ == '__main__':
    tree = update_mets(*sys.argv[1:])
    tree.write('mets.xml', pretty_print=True)
