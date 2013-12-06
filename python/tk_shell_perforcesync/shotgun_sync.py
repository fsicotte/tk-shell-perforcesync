# Copyright (c) 2013 Shotgun Software Inc.
# 
# CONFIDENTIAL AND PROPRIETARY
# 
# This work is provided "AS IS" and subject to the Shotgun Pipeline Toolkit 
# Source Code License included in this distribution package. See LICENSE.
# By accessing, using, copying or modifying this work you indicate your 
# agreement to the Shotgun Pipeline Toolkit Source Code License. All rights 
# not expressly granted therein are reserved by Shotgun Software Inc.

"""
"""

import os
import sys

import sgtk
from P4 import P4Exception

class ShotgunSync(object):
    """
    """
    
    def __init__(self, app):
        """
        """
        self._app = app
        
        # some useful cache info:        
        self._project_roots = set()
        self._project_pc_roots = {}
        self._pc_tk_instances = {}
        self._sg_user_lookup = {}
        
    def sync_changes(self, start_change, end_change, p4=None):
        """
        Sync range of changes:
        """
        self._app.log_info("Syncing changes %d - %d..." % (start_change, end_change))
        
        # connect to Perforce:
        p4 = p4 or self._connect_to_perforce()
        if p4:
            try:
                # sync changes:
                for change_id in range(start_change, end_change+1):
                    try:
                        self.sync_change(change_id, p4)
                    except Exception, e:
                        self._app.log_error("Failed to sync change %d: %s" % (change_id, e))   
            finally:
                # always disconnect:
                p4.disconnect()

    def sync_change(self, change_id, p4=None):
        """
        """
        self._app.log_info("Syncing change %d" % change_id)
        
        # connect to perforce:
        p4 = p4 or self._connect_to_perforce()
    
        try:    
            # get the change details:
            p4_res = p4.run_describe(change_id)
    
            if not p4_res:
                return
            
            # p4_res = [
            #  {'status': 'submitted', 
            #   'fileSize': ['368095', '368097'], 
            #   'changeType': 'public', 
            #   'rev': ['3', '2'], 
            #   'client': 'ad_zombie_racer', 
            #   'user': 'Alan', 
            #   'time': '1382901628', 
            #   'action': ['edit', 'edit'], 
            #   'path': '//depot/projects/zombie_racer_5/master/assets/Environment/Track_Straight/Model/work/maya/*', 
            #   'digest': ['5BA1FC1E208D11BFE5C9CF61449026FF', 'DB747894FC9FE007A04863FEEA71CB17'], 
            #   'type': ['xtext', 'text'], 
            #   'depotFile': ['//depot/projects/zombie_racer_5/master/assets/Environment/Track_Straight/Model/work/maya/TrackStraight.ma', 
            #                 '//depot/projects/zombie_racer_5/master/assets/Environment/Track_Straight/Model/work/maya/TrackStraightB.ma'], 
            #   'change': '37', 
            #   'desc': 'lets submit a couple of files!\n'}
            # ]
            change_desc = p4_res[0]
            change_id = change_desc["change"]
            
            # get shotgun user:
            sg_user = self._get_sg_user(self._app.sgtk, change_desc["user"])
            
            # process all file revisions in change:
            published_files = []
            file_revs = zip(change_desc.get("depotFile", []), change_desc.get("rev", []))
            for depot_path, rev in file_revs:
                # process file revision
                file_details = self._process_file_revision(depot_path, rev, sg_user, change_id, change_desc.get("desc", ""), p4)
            
                if file_details:
                    published_files.append(file_details)            
            
            # and create entity for change:
            change = {}
            change["code"] = change_id
            change["description"] = change_desc.get("desc", "")
            change["project"] = self._app.context.project
            change["created_by"] = sg_user
            change["sg_published_files"] = [{"type":"PublishedFile", "id":file["sg_file"]["id"]} for file in published_files]
            self._create_or_update_change(change)
            
        except Exception, e:
            self._app.log_error(e)
            self._app.log_exception("AARGH")    

    def _process_file_revision(self, depot_path, file_revision, sg_user, change_id, description, p4):
        """
        Process a single revision of a perforce file.  If the file can be mapped to the
        current context's project then create a published file for it and return the
        details. 
        """
        # Determine the depot project root for this depot file:
        depot_project_root = self._get_depot_project_root(depot_path, p4)
        
        # find the pipeline config root from the depot root:
        local_pc_root = self._get_local_pc_root(depot_project_root, p4)
        
        # get a tk instance for this pc root:
        tk = self._pc_tk_instances.get(local_pc_root)
        if not tk:
            tk = sgtk.sgtk_from_path(local_pc_root)
            self._pc_tk_instances[local_pc_root] = tk
        
        # from this, get the shotgun project id:
        project_id = tk.pipeline_configuration.get_project_id()
        
        # and check that this is the same project we're running in:
        if project_id != self._app.context.project["id"]:
            # it isn't so we can skip this file:
            return None
        
        # it is so lets Register a new Published File for it
        #
                        
        # build a context for the depot path:
        # (AD) - this is obviously very fragile so need a way to do this using depot paths
        # - maybe be able to set the project root and then set it to depot_project_root?
        local_path = tk.pipeline_configuration.get_primary_data_root() + depot_path[len(depot_project_root):]
        context = self._get_context_for_path(tk, local_path)    
        
        # Register publish for the file:
        self._app.log_debug("Registering new published file: %s:%s" % (depot_path, file_revision))
        sg_res = sgtk.util.register_publish(self._app.sgtk,
                                            context, 
                                            depot_path, 
                                            os.path.basename(local_path), 
                                            int(file_revision), 
                                            comment = description, 
                                            created_by = sg_user)
        
        file_data = {"depot":depot_path, "local":local_path, "context":context, "sg_file":sg_res}
        
        return file_data

    def _create_or_update_change(self, change):
        """
        """
        # if entity already exists then we just want to update it.  Otherwise we'll
        # end up with multiple entities for the same change!
        filters = [["code", "is", change["code"]], ["project", "is", change["project"]]]
        sg_res = self._app.shotgun.find_one("Revision", filters)
        if sg_res:
            # update existing change:
            self._app.log_debug("Updating existing Change (Revision) entity: %d" % sg_res["id"])
            # ...
            
        else:
            # create new change:
            self._app.log_debug("Creating new Change (Revision) entity for change: %s" % data["code"])
            self._app.shotgun.create("Revision", change)

    def _connect_to_perforce(self):
        """
        Connect to Perforce
        """
        try:
            p4_fw = sgtk.platform.get_framework("tk-framework-perforce")
            p4 = p4_fw.connect(False)
            return p4
        except:
            self._app.log_exception("Failed to connect!")
            return None
        
    def _get_sg_user(self, tk, perforce_user):
        """
        Get the Shotgun user for the specified Perforce user
        """
        if perforce_user not in self._sg_user_lookup:
            # user not in lookup so ask framework:
            p4_fw = sgtk.platform.get_framework("tk-framework-perforce")
            sg_user = p4_fw.get_shotgun_user(perforce_user)
            self._sg_user_lookup[perforce_user] = sg_user

        return self._sg_user_lookup[perforce_user] 
        
    def _get_context_for_path(self, tk, local_path):
        """
        Use hook to construct context for the path
        """
        # (TODO) - the context should ideally be preserved through the metadata file when published.
        # However, still need to handle the case where the file may have been submitted directly through
        # Perforce so will probably still want to have hook for this...
        #
        # We could do something intelligent like using the previous context information if this
        # path has been published before? - would this be in the hook or before that? - Probably try
        # to construct the best possible context and then allow a hook to create a new context if it needs
        # to? 
        
        context =self._app.sgtk.context_from_path(local_path)
                
        # if we don't have a task but do have a step then try to determine the task from the step:
        # (TODO) - this logic should be moved to a hook as it  won't work if there are Multiple tasks on 
        # the same entity that use the same Step!
        if not context.task:
            if context.entity and context.step:
                sg_res = self._app.shotgun.find("Task", [["step", "is", context.step], ["entity", "is", context.entity]])
                if sg_res and len(sg_res) == 1:
                    context = self._app.sgtk.context_from_entity(sg_res[0]["type"], sg_res[0]["id"])
        
        return context
        
    def _get_depot_project_root(self, depot_path, p4):
        """
        Find the depot-relative project root for the specified depot file
        """
        # first, check to see if depot_path is under a known project root:
        for pr in self._project_roots:
            if depot_path.startswith(pr):
                return pr
        
        # start search from project root:
        project_root = depot_path
        
        while len(project_root):
            project_root = project_root[:project_root.rfind("/") or 0].rstrip("/")
            tank_configs_path = "%s/tank/config/tank_configs.yml" % project_root
            try:
                # see if this file exists in the depot:
                p4_res = p4.run_files(tank_configs_path)
                if p4_res:
                    # it does - win!
                    break
            except P4Exception:
                # ignore perforce exceptions!
                pass
        
        # cache project root for next time:
        if project_root:
            self._project_roots.add(project_root)
        
        return project_root
    
    
    def _get_local_pc_root(self, project_root, p4):
        """
        Determine the local pipeline configuration directory
        for the given depot project_root... 
        """

        # first, check to see if info is in cache:
        pc_root = self._project_pc_roots.get(project_root)
        if pc_root != None:
            return pc_root
        self._project_pc_roots[project_root] = ""
        
        # check that the tank_configs.yml file is in the correct place:
        tank_configs_path = "%s/tank/config/tank_configs.yml" % project_root
        try:
            p4.run_files(tank_configs_path)
        except P4Exception:
            # bad - file not found!
            return None

        # read the pc root path from the config file:
        try:
            p4_res = p4.run_print(tank_configs_path)
            if not p4_res:
                return None
            
            # [{'rev': '1', ...}, "- {darwin: /toolkit_perforce/shotgun/zombie_racer_5, ..."]
            contents = p4_res[1]

            from tank_vendor import yaml
            config = yaml.load(contents)
            
            local_pc_root = config[0][sys.platform]
            
        except Exception, e:
            self._app.log_error("Failed to determine project root: %s" % e)
            # any exception is bad!
            return None
        
        # cache in case we need it again:
        self._project_pc_roots[project_root] = local_pc_root
        
        return local_pc_root 
            