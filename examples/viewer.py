# Copyright (c) Meta Platforms, Inc. and its affiliates.
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

import ctypes
import json
import math
import os
import string
import sys
import time
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple

flags = sys.getdlopenflags()
sys.setdlopenflags(flags | ctypes.RTLD_GLOBAL)

import habitat.datasets.rearrange.samplers.receptacle as hab_receptacle
import habitat.sims.habitat_simulator.sim_utilities as sutils
import magnum as mn
import numpy as np
from habitat.datasets.rearrange.navmesh_utils import get_largest_island_index
from habitat.datasets.rearrange.samplers.object_sampler import ObjectSampler
from magnum import shaders, text
from magnum.platform.glfw import Application

import habitat_sim
from habitat_sim import ReplayRenderer, ReplayRendererConfiguration, physics
from habitat_sim.logging import LoggingContext, logger
from habitat_sim.utils.common import quat_from_angle_axis
from habitat_sim.utils.settings import default_sim_settings, make_cfg

# add tools directory so I can import things to try them in the viewer
sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools"))
print(sys.path)
import collision_shape_automation as csa

# CollisionProxyOptimizer initialized before the application
_cpo: Optional[csa.CollisionProxyOptimizer] = None
_cpo_threads = []


def _cpo_initialized():
    global _cpo
    global _cpo_threads
    if _cpo is None:
        return False
    return all(not thread.is_alive() for thread in _cpo_threads)


class RecColorMode(Enum):
    """
    Defines the coloring mode for receptacle debug drawing.
    """

    DEFAULT = 0  # all magenta
    GT_ACCESS = 1  # red to green
    GT_STABILITY = 2
    PR_ACCESS = 3
    PR_STABILITY = 4
    FILTERING = 5  # colored by filter status (green=active, yellow=manually filtered, red=automatically filtered (access), magenta=automatically filtered (access), blue=automatically filtered (height))


class ColorLERP:
    """
    xyz lerp between two colors.
    """

    def __init__(self, c0: mn.Color4, c1: mn.Color4):
        self.c0 = c0.to_xyz()
        self.c1 = c1.to_xyz()
        self.delta = self.c1 - self.c0

    def at(self, t: float) -> mn.Color4:
        """
        Compute the LERP at time t [0,1].
        """
        assert t >= 0 and t <= 1, "Extrapolation not recommended in color space."
        t_color_xyz = self.c0 + self.delta * t
        return mn.Color4.from_xyz(t_color_xyz)


# red to green lerp for heatmaps
rg_lerp = ColorLERP(mn.Color4.red(), mn.Color4.green())


class HabitatSimInteractiveViewer(Application):
    # the maximum number of chars displayable in the app window
    # using the magnum text module. These chars are used to
    # display the CPU/GPU usage data
    MAX_DISPLAY_TEXT_CHARS = 256

    # how much to displace window text relative to the center of the
    # app window (e.g if you want the display text in the top left of
    # the app window, you will displace the text
    # window width * -TEXT_DELTA_FROM_CENTER in the x axis and
    # window height * TEXT_DELTA_FROM_CENTER in the y axis, as the text
    # position defaults to the middle of the app window)
    TEXT_DELTA_FROM_CENTER = 0.49

    # font size of the magnum in-window display text that displays
    # CPU and GPU usage info
    DISPLAY_FONT_SIZE = 16.0

    def __init__(
        self,
        sim_settings: Dict[str, Any],
        mm: Optional[habitat_sim.metadata.MetadataMediator] = None,
    ) -> None:
        self.sim_settings: Dict[str:Any] = sim_settings

        self.enable_batch_renderer: bool = self.sim_settings["enable_batch_renderer"]
        self.num_env: int = (
            self.sim_settings["num_environments"] if self.enable_batch_renderer else 1
        )

        # Compute environment camera resolution based on the number of environments to render in the window.
        window_size: mn.Vector2 = (
            self.sim_settings["window_width"],
            self.sim_settings["window_height"],
        )

        configuration = self.Configuration()
        configuration.title = "Habitat Sim Interactive Viewer"
        configuration.size = window_size
        Application.__init__(self, configuration)
        self.fps: float = 60.0

        # Compute environment camera resolution based on the number of environments to render in the window.
        grid_size: mn.Vector2i = ReplayRenderer.environment_grid_size(self.num_env)
        camera_resolution: mn.Vector2 = mn.Vector2(self.framebuffer_size) / mn.Vector2(
            grid_size
        )
        self.sim_settings["width"] = camera_resolution[0]
        self.sim_settings["height"] = camera_resolution[1]

        # draw Bullet debug line visualizations (e.g. collision meshes)
        self.debug_bullet_draw = False
        # draw active contact point debug line visualizations
        self.contact_debug_draw = False
        # draw semantic region debug visualizations if present
        self.semantic_region_debug_draw = False

        # cache most recently loaded URDF file for quick-reload
        self.cached_urdf = ""

        # set up our movement map
        key = Application.KeyEvent.Key
        self.pressed = {
            key.UP: False,
            key.DOWN: False,
            key.LEFT: False,
            key.RIGHT: False,
            key.A: False,
            key.D: False,
            key.S: False,
            key.W: False,
            key.X: False,
            key.Z: False,
        }

        # set up our movement key bindings map
        key = Application.KeyEvent.Key
        self.key_to_action = {
            key.UP: "look_up",
            key.DOWN: "look_down",
            key.LEFT: "turn_left",
            key.RIGHT: "turn_right",
            key.A: "move_left",
            key.D: "move_right",
            key.S: "move_backward",
            key.W: "move_forward",
            key.X: "move_down",
            key.Z: "move_up",
        }

        # Load a TrueTypeFont plugin and open the font file
        self.display_font = text.FontManager().load_and_instantiate("TrueTypeFont")
        relative_path_to_font = "../data/fonts/ProggyClean.ttf"
        self.display_font.open_file(
            os.path.join(os.path.dirname(__file__), relative_path_to_font),
            13,
        )

        # Glyphs we need to render everything
        self.glyph_cache = text.GlyphCache(mn.Vector2i(256), mn.Vector2i(1))
        self.display_font.fill_glyph_cache(
            self.glyph_cache,
            string.ascii_lowercase
            + string.ascii_uppercase
            + string.digits
            + ":-_+,.! %µ",
        )

        # magnum text object that displays CPU/GPU usage data in the app window
        self.window_text = text.Renderer2D(
            self.display_font,
            self.glyph_cache,
            HabitatSimInteractiveViewer.DISPLAY_FONT_SIZE,
            text.Alignment.TOP_LEFT,
        )
        self.window_text.reserve(HabitatSimInteractiveViewer.MAX_DISPLAY_TEXT_CHARS)

        # text object transform in window space is Projection matrix times Translation Matrix
        # put text in top left of window
        self.window_text_transform = mn.Matrix3.projection(
            self.framebuffer_size
        ) @ mn.Matrix3.translation(
            mn.Vector2(self.framebuffer_size)
            * mn.Vector2(
                -HabitatSimInteractiveViewer.TEXT_DELTA_FROM_CENTER,
                HabitatSimInteractiveViewer.TEXT_DELTA_FROM_CENTER,
            )
        )
        self.shader = shaders.VectorGL2D()

        # Set blend function
        mn.gl.Renderer.set_blend_equation(
            mn.gl.Renderer.BlendEquation.ADD, mn.gl.Renderer.BlendEquation.ADD
        )

        # variables that track app data and CPU/GPU usage
        self.num_frames_to_track = 60

        # Cycle mouse utilities
        self.mouse_interaction = MouseMode.LOOK
        self.mouse_grabber: Optional[MouseGrabber] = None
        self.previous_mouse_point = None

        # toggle physics simulation on/off
        self.simulating = False

        # receptacle visualization
        self.receptacles = None
        self.display_receptacles = False
        global _cpo
        self._cpo = _cpo
        self.cpo_initialized = False
        self.show_filtered = True
        self.rec_access_filter_threshold = 0.12  # empirically chosen
        self.rec_color_mode = RecColorMode.FILTERING
        # map receptacle to parent objects
        self.rec_to_poh: Dict[hab_receptacle.Receptacle, str] = {}
        # contains filtering metadata and classification of meshes filtered automatically and manually
        self.rec_filter_data = None
        self.rec_filter_path = self.sim_settings["rec_filter_file"]

        # display stability samples for selected object w/ receptacle
        self.display_selected_stability_samples = True

        # collision proxy visualization
        self.col_proxy_objs = None
        self.col_proxies_visible = True
        self.original_objs_visible = True
        self.proxy_obj_postfix = "_collision_stand-in"

        # mouse raycast visualization
        self.mouse_cast_results = None
        # last clicked or None for stage
        self.selected_object = None
        self.selected_rec = None
        self.ao_link_map = None

        # marker set cache to prevent parsing metadata every frame
        self.marker_sets: Dict[str, Dict[int, Dict[str, List[mn.Vector3]]]] = None
        self.debug_random_colors: List[mn.Color4] = []
        self.selected_marker_set_index = 0

        # toggle a single simulation step at the next opportunity if not
        # simulating continuously.
        self.simulate_single_step = False

        # configure our simulator
        self.cfg: Optional[habitat_sim.simulator.Configuration] = None
        self.sim: Optional[habitat_sim.simulator.Simulator] = None
        self.tiled_sims: list[habitat_sim.simulator.Simulator] = None
        self.replay_renderer_cfg: Optional[ReplayRendererConfiguration] = None
        self.replay_renderer: Optional[ReplayRenderer] = None
        self.reconfigure_sim(mm)
        self.debug_semantic_colors = {}
        self.load_scene_filter_file()

        # -----------------------------------------
        # Clutter Generation Integration:
        self.clutter_object_set = [
            "002_master_chef_can",
            "003_cracker_box",
            "004_sugar_box",
            "005_tomato_soup_can",
            "007_tuna_fish_can",
            "008_pudding_box",
            "009_gelatin_box",
            "010_potted_meat_can",
            "024_bowl",
        ]
        self.clutter_object_handles = []
        self.clutter_object_instances = []
        # cache initial states for classification of unstable objects
        self.clutter_object_initial_states = []
        self.num_unstable_objects = 0
        # add some clutter objects to the MM
        self.sim.metadata_mediator.object_template_manager.load_configs(
            "data/objects/ycb/configs/"
        )
        # -----------------------------------------

        # compute NavMesh if not already loaded by the scene.
        if (
            not self.sim.pathfinder.is_loaded
            and self.cfg.sim_cfg.scene_id.lower() != "none"
        ):
            self.navmesh_config_and_recompute()

        self.time_since_last_simulation = 0.0
        LoggingContext.reinitialize_from_env()
        logger.setLevel("INFO")
        self.print_help_text()

    def modify_param_from_term(self):
        """
        Prompts the user to enter an attribute name and new value.
        Attempts to fulfill the user's request.
        """
        # first get an attribute
        user_attr = input("++++++++++++\nProvide an attribute to edit: ")
        if not hasattr(self, user_attr):
            print(f" The '{user_attr}' attribute does not exist.")
            return

        # then get a value
        user_val = input(f"Now provide a value for '{user_attr}': ")
        cur_attr_val = getattr(self, user_attr)
        if cur_attr_val is not None:
            try:
                # try type conversion
                new_val = type(cur_attr_val)(user_val)

                # special handling for bool because all strings become True with cast
                if isinstance(cur_attr_val, bool):
                    if user_val.lower() == "false":
                        new_val = False
                    elif user_val.lower() == "true":
                        new_val = True

                setattr(self, user_attr, new_val)
                print(
                    f"attr '{user_attr}' set to '{getattr(self, user_attr)}' (type={type(new_val)})."
                )
            except Exception:
                print(f"Failed to cast '{user_val}' to {type(cur_attr_val)}.")
        else:
            print("That attribute is unset, so I don't know the type.")

    def load_scene_filter_file(self):
        """
        Load the filter file for a scene from config.
        """

        scene_user_defined = self.sim.metadata_mediator.get_scene_user_defined(
            self.sim.curr_scene_name
        )
        if scene_user_defined is not None and scene_user_defined.has_value(
            "scene_filter_file"
        ):
            scene_filter_file = scene_user_defined.get("scene_filter_file")
            # construct the dataset level path for the filter data file
            scene_filter_file = os.path.join(
                os.path.dirname(mm.active_dataset), scene_filter_file
            )
            print(f"scene_filter_file = {scene_filter_file}")
            self.load_receptacles()
            self.load_filtered_recs(scene_filter_file)
            self.rec_filter_path = scene_filter_file
        else:
            print(
                f"WARNING: No rec filter file configured for scene {self.sim.curr_scene_name}."
            )

    def get_rec_instance_name(self, receptacle: hab_receptacle.Receptacle) -> str:
        """
        Gets a unique string name for the Receptacle instance.
        Multiple Receptacles can share a name (e.g. if the object has multiple instances in the scene).
        The unique name is constructed as '<object instance name>|<receptacle name>'.
        """
        rec_unique_name = receptacle.parent_object_handle + "|" + receptacle.name
        return rec_unique_name

    def get_closest_tri_receptacle(
        self, pos: mn.Vector3, max_dist: float = 3.5
    ) -> Optional[hab_receptacle.TriangleMeshReceptacle]:
        """
        Return the closest receptacle to the given position or None.

        :param pos: The point to compare with receptacle verts.
        :param max_dist: The maximum allowable distance to the receptacle to count.

        :return: None if failed or closest receptacle.
        """
        if self.receptacles is None or not self.display_receptacles:
            return None
        closest_rec = None
        closest_rec_dist = max_dist
        for receptacle in self.receptacles:
            g_trans = receptacle.get_global_transform(self.sim)
            # transform the query point once instead of all verts
            local_point = g_trans.inverted().transform_point(pos)
            if (g_trans.translation - pos).length() < max_dist:
                # receptacles object transform should be close to the point
                for vert in receptacle.mesh_data.attribute(
                    mn.trade.MeshAttribute.POSITION
                ):
                    v_dist = (local_point - vert).length()
                    if v_dist < closest_rec_dist:
                        closest_rec_dist = v_dist
                        closest_rec = receptacle
        return closest_rec

    def compute_rec_filter_state(
        self,
        access_threshold: float = 0.12,
        stab_threshold: float = 0.5,
        filter_shape: str = "pr0",
    ) -> None:
        """
        Check all receptacles against automated filters to fill the

        :param access_threshold: Access threshold for filtering. Roughly % of sample points with some raycast access.
        :param stab_threshold: Stability threshold for filtering. Roughly % of sample points with stable object support.
        :param filter_shape: Which shape metrics to use for filter. Choices typically "gt"(ground truth) or "pr0"(proxy shape).
        """
        # load receptacles if not done
        if self.receptacles is None:
            self.load_receptacles()
        assert (
            self._cpo is not None
        ), "Must initialize the CPO before automatic filtering. Re-run with '--init-cpo'."

        # initialize if necessary
        if self.rec_filter_data is None:
            self.rec_filter_data = {
                "active": [],
                "manually_filtered": [],
                "access_filtered": [],
                "access_threshold": access_threshold,  # set in filter procedure
                "stability_filtered": [],
                "stability threshold": stab_threshold,  # set in filter procedure
                # TODO:
                "height_filtered": [],
                "max_height": 0,
                "min_height": 0,
            }

        for rec in self.receptacles:
            rec_unique_name = self.get_rec_instance_name(rec)
            # respect already marked receptacles
            if rec_unique_name not in self.rec_filter_data["manually_filtered"]:
                rec_dat = self._cpo.gt_data[self.rec_to_poh[rec]]["receptacles"][
                    rec.name
                ]
                rec_shape_data = rec_dat["shape_id_results"][filter_shape]
                # filter by access
                if (
                    "access_results" in rec_shape_data
                    and rec_shape_data["access_results"]["receptacle_access_score"]
                    < access_threshold
                ):
                    self.rec_filter_data["access_filtered"].append(rec_unique_name)
                # filter by stability
                elif (
                    "stability_results" in rec_shape_data
                    and rec_shape_data["stability_results"]["success_ratio"]
                    < stab_threshold
                ):
                    self.rec_filter_data["stability_filtered"].append(rec_unique_name)
                # TODO: add more filters
                # TODO: 1. filter by height relative to the floor
                # TODO: 2. filter outdoor (raycast up)
                # TODO: 3/4: filter by access/stability in scene context (relative to other objects)
                # remaining receptacles are active
                else:
                    self.rec_filter_data["active"].append(rec_unique_name)

    def export_filtered_recs(self, filepath: Optional[str] = None) -> None:
        """
        Save a JSON with filtering metadata and filtered Receptacles for a scene.

        :param filepath: Defines the output filename for this JSON. If omitted, defaults to "./rec_filter_data.json".
        """
        if filepath is None:
            filepath = "rec_filter_data.json"
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        with open(filepath, "w") as f:
            f.write(json.dumps(self.rec_filter_data, indent=2))
        print(f"Exported filter annotations to {filepath}.")

    def load_filtered_recs(self, filepath: Optional[str] = None) -> None:
        """
        Load a Receptacle filtering metadata JSON to visualize the state of the scene.

        :param filepath: Defines the input filename for this JSON. If omitted, defaults to "./rec_filter_data.json".
        """
        if filepath is None:
            filepath = "rec_filter_data.json"
        if not os.path.exists(filepath):
            print(f"Filtered rec metadata file {filepath} does not exist. Cannot load.")
            return
        with open(filepath, "r") as f:
            self.rec_filter_data = json.load(f)

        # assert the format is correct
        assert "active" in self.rec_filter_data
        assert "manually_filtered" in self.rec_filter_data
        assert "access_filtered" in self.rec_filter_data
        assert "stability_filtered" in self.rec_filter_data
        assert "height_filtered" in self.rec_filter_data
        print(f"Loaded filter annotations from {filepath}")

    def load_receptacles(self):
        """
        Load all receptacle data and setup helper datastructures.
        """
        self.receptacles = hab_receptacle.find_receptacles(self.sim)
        self.receptacles = [
            rec
            for rec in self.receptacles
            if "collision_stand-in" not in rec.parent_object_handle
        ]
        for receptacle in self.receptacles:
            if receptacle not in self.rec_to_poh:
                po_handle = sutils.get_obj_from_handle(
                    self.sim, receptacle.parent_object_handle
                ).creation_attributes.handle
                self.rec_to_poh[receptacle] = po_handle

    def add_col_proxy_object(
        self, obj_instance: habitat_sim.physics.ManagedRigidObject
    ) -> habitat_sim.physics.ManagedRigidObject:
        """
        Add a collision object visualization proxy to the scene overlapping with the given object.
        Return the new proxy object.
        """
        # replace the object with a collision_object
        obj_temp_handle = obj_instance.creation_attributes.handle
        otm = self.sim.get_object_template_manager()
        object_template = otm.get_template_by_handle(obj_temp_handle)
        object_template.scale = obj_instance.scale + np.ones(3) * 0.01
        object_template.render_asset_handle = object_template.collision_asset_handle
        object_template.is_collidable = False
        reg_id = otm.register_template(
            object_template,
            object_template.handle + self.proxy_obj_postfix,
        )
        ro_mngr = self.sim.get_rigid_object_manager()
        new_obj = ro_mngr.add_object_by_template_id(reg_id)
        new_obj.motion_type = habitat_sim.physics.MotionType.KINEMATIC
        new_obj.translation = obj_instance.translation
        new_obj.rotation = obj_instance.rotation
        self.sim.set_object_bb_draw(True, new_obj.object_id)
        return new_obj

    def draw_contact_debug(self, debug_line_render: Any):
        """
        This method is called to render a debug line overlay displaying active contact points and normals.
        Yellow lines show the contact distance along the normal and red lines show the contact normal at a fixed length.
        """
        yellow = mn.Color4.yellow()
        red = mn.Color4.red()
        cps = self.sim.get_physics_contact_points()
        debug_line_render.set_line_width(1.5)
        camera_position = self.render_camera.render_camera.node.absolute_translation
        # only showing active contacts
        active_contacts = (x for x in cps if x.is_active)
        for cp in active_contacts:
            # red shows the contact distance
            debug_line_render.draw_transformed_line(
                cp.position_on_b_in_ws,
                cp.position_on_b_in_ws
                + cp.contact_normal_on_b_in_ws * -cp.contact_distance,
                red,
            )
            # yellow shows the contact normal at a fixed length for visualization
            debug_line_render.draw_transformed_line(
                cp.position_on_b_in_ws,
                # + cp.contact_normal_on_b_in_ws * cp.contact_distance,
                cp.position_on_b_in_ws + cp.contact_normal_on_b_in_ws * 0.1,
                yellow,
            )
            debug_line_render.draw_circle(
                translation=cp.position_on_b_in_ws,
                radius=0.005,
                color=yellow,
                normal=camera_position - cp.position_on_b_in_ws,
            )

    def draw_marker_sets_debug(self, debug_line_render: Any) -> None:
        """
        Draw the global state of all configured marker sets.
        """
        camera_position = self.render_camera.render_camera.node.absolute_translation
        marker_set_counter = 0
        for ao_handle in self.marker_sets:
            ao = sutils.get_obj_from_handle(self.sim, ao_handle)
            for link_id in self.marker_sets[ao_handle]:
                for marker_set_name in self.marker_sets[ao_handle][link_id]:
                    if len(self.debug_random_colors) <= marker_set_counter:
                        self.debug_random_colors.append(
                            mn.Color4(mn.Vector3(np.random.random(3)))
                        )
                    marker_set_color = self.debug_random_colors[marker_set_counter]
                    marker_set_counter += 1
                    global_marker_set = sutils.get_global_link_marker_set(
                        ao, link_id, marker_set_name, self.marker_sets[ao_handle]
                    )
                    for global_marker_pos in global_marker_set:
                        debug_line_render.draw_circle(
                            translation=global_marker_pos,
                            radius=0.005,
                            color=marker_set_color,
                            normal=camera_position - global_marker_pos,
                        )

    def draw_region_debug(self, debug_line_render: Any) -> None:
        """
        Draw the semantic region wireframes.
        """

        for region in self.sim.semantic_scene.regions:
            color = self.debug_semantic_colors.get(region.id, mn.Color4.magenta())
            for edge in region.volume_edges:
                debug_line_render.draw_transformed_line(
                    edge[0],
                    edge[1],
                    color,
                )

    def debug_draw(self):
        """
        Additional draw commands to be called during draw_event.
        """
        self.sim.get_rigid_object_manager()
        if self.debug_bullet_draw:
            render_cam = self.render_camera.render_camera
            proj_mat = render_cam.projection_matrix.__matmul__(render_cam.camera_matrix)
            self.sim.physics_debug_draw(proj_mat)

        debug_line_render = self.sim.get_debug_line_render()
        if self.contact_debug_draw:
            self.draw_contact_debug(debug_line_render)
        if self.semantic_region_debug_draw:
            if len(self.debug_semantic_colors) != len(self.sim.semantic_scene.regions):
                for region in self.sim.semantic_scene.regions:
                    self.debug_semantic_colors[region.id] = mn.Color4(
                        mn.Vector3(np.random.random(3))
                    )
            self.draw_region_debug(debug_line_render)
        if self.marker_sets is not None:
            self.draw_marker_sets_debug(debug_line_render)
        if self.receptacles is not None and self.display_receptacles:
            if self.rec_filter_data is None and self.cpo_initialized:
                self.compute_rec_filter_state(
                    access_threshold=self.rec_access_filter_threshold
                )
            c_pos = self.render_camera.node.absolute_translation
            c_forward = (
                self.render_camera.node.absolute_transformation().transform_vector(
                    mn.Vector3(0, 0, -1)
                )
            )
            for receptacle in self.receptacles:
                rec_unique_name = self.get_rec_instance_name(receptacle)
                # filter all non-active receptacles
                if (
                    self.rec_filter_data is not None
                    and not self.show_filtered
                    and rec_unique_name not in self.rec_filter_data["active"]
                ):
                    continue

                rec_dat = None
                if self.cpo_initialized:
                    rec_dat = self._cpo.gt_data[self.rec_to_poh[receptacle]][
                        "receptacles"
                    ][receptacle.name]

                r_trans = receptacle.get_global_transform(self.sim)
                # display point samples for selected object
                if (
                    rec_dat is not None
                    and self.display_selected_stability_samples
                    and self.selected_object is not None
                    and self.selected_object.handle == receptacle.parent_object_handle
                ):
                    # display colored circles for stability samples on the selected object
                    point_metric_dat = rec_dat["shape_id_results"]["gt"][
                        "access_results"
                    ]["receptacle_point_access_scores"]
                    if self.rec_color_mode == RecColorMode.GT_STABILITY:
                        point_metric_dat = rec_dat["shape_id_results"]["gt"][
                            "stability_results"
                        ]["point_stabilities"]
                    elif self.rec_color_mode == RecColorMode.PR_STABILITY:
                        point_metric_dat = rec_dat["shape_id_results"]["pr0"][
                            "stability_results"
                        ]["point_stabilities"]
                    elif self.rec_color_mode == RecColorMode.PR_ACCESS:
                        point_metric_dat = rec_dat["shape_id_results"]["pr0"][
                            "access_results"
                        ]["receptacle_point_access_scores"]

                    for point_metric, point in zip(
                        point_metric_dat,
                        rec_dat["sample_points"],
                    ):
                        self.sim.get_debug_line_render().draw_circle(
                            translation=r_trans.transform_point(point),
                            radius=0.02,
                            normal=mn.Vector3(0, 1, 0),
                            color=rg_lerp.at(point_metric),
                            num_segments=12,
                        )

                rec_obj = sutils.get_obj_from_handle(
                    self.sim, receptacle.parent_object_handle
                )
                key_points = [r_trans.translation]
                key_points.extend(
                    sutils.get_bb_corners(rec_obj.root_scene_node.cumulative_bb)
                )

                in_view = False
                for ix, key_point in enumerate(key_points):
                    r_pos = key_point
                    if ix > 0:
                        r_pos = rec_obj.transformation.transform_point(key_point)
                    c_to_r = r_pos - c_pos
                    # only display receptacles within 4 meters centered in view
                    if (
                        c_to_r.length() < 4
                        and mn.math.dot((c_to_r).normalized(), c_forward) > 0.7
                    ):
                        in_view = True
                        break
                if in_view:
                    # handle coloring
                    rec_color = None
                    if self.selected_rec == receptacle:
                        # white
                        rec_color = mn.Color4.cyan()
                    elif (
                        self.rec_filter_data is not None
                    ) and self.rec_color_mode == RecColorMode.FILTERING:
                        # blue indicates no filter data for the receptacle, it may be newer than the filter file.
                        rec_color = mn.Color4.blue()
                        if rec_unique_name in self.rec_filter_data["active"]:
                            rec_color = mn.Color4.green()
                        elif (
                            rec_unique_name in self.rec_filter_data["manually_filtered"]
                        ):
                            rec_color = mn.Color4.yellow()
                        elif rec_unique_name in self.rec_filter_data["access_filtered"]:
                            rec_color = mn.Color4.red()
                        elif (
                            rec_unique_name
                            in self.rec_filter_data["stability_filtered"]
                        ):
                            rec_color = mn.Color4.magenta()
                        elif rec_unique_name in self.rec_filter_data["height_filtered"]:
                            rec_color = mn.Color4.blue()
                    elif (
                        self.cpo_initialized
                        and self.rec_color_mode != RecColorMode.DEFAULT
                    ):
                        if self.rec_color_mode == RecColorMode.GT_STABILITY:
                            rec_color = rg_lerp.at(
                                rec_dat["shape_id_results"]["gt"]["stability_results"][
                                    "success_ratio"
                                ]
                            )
                        elif self.rec_color_mode == RecColorMode.GT_ACCESS:
                            rec_color = rg_lerp.at(
                                rec_dat["shape_id_results"]["gt"]["access_results"][
                                    "receptacle_access_score"
                                ]
                            )
                        elif self.rec_color_mode == RecColorMode.PR_STABILITY:
                            rec_color = rg_lerp.at(
                                rec_dat["shape_id_results"]["pr0"]["stability_results"][
                                    "success_ratio"
                                ]
                            )
                        elif self.rec_color_mode == RecColorMode.PR_ACCESS:
                            rec_color = rg_lerp.at(
                                rec_dat["shape_id_results"]["pr0"]["access_results"][
                                    "receptacle_access_score"
                                ]
                            )

                    receptacle.debug_draw(self.sim, color=rec_color)
                    if True:
                        dblr = self.sim.get_debug_line_render()
                        t_form = receptacle.get_global_transform(self.sim)
                        dblr.push_transform(t_form)
                        dblr.draw_transformed_line(
                            mn.Vector3(0), receptacle.up, mn.Color4.cyan()
                        )
                        dblr.pop_transform()
        # mouse raycast circle
        white = mn.Color4(mn.Vector3(1.0), 1.0)
        if self.mouse_cast_results is not None and self.mouse_cast_results.has_hits():
            self.sim.get_debug_line_render().draw_circle(
                translation=self.mouse_cast_results.hits[0].point,
                radius=0.005,
                color=white,
                normal=self.mouse_cast_results.hits[0].normal,
            )

    def draw_event(
        self,
        simulation_call: Optional[Callable] = None,
        global_call: Optional[Callable] = None,
        active_agent_id_and_sensor_name: Tuple[int, str] = (0, "color_sensor"),
    ) -> None:
        """
        Calls continuously to re-render frames and swap the two frame buffers
        at a fixed rate.
        """
        # until cpo initialization is finished, keep checking
        if not self.cpo_initialized:
            self.cpo_initialized = _cpo_initialized()

        agent_acts_per_sec = self.fps

        mn.gl.default_framebuffer.clear(
            mn.gl.FramebufferClear.COLOR | mn.gl.FramebufferClear.DEPTH
        )

        # Agent actions should occur at a fixed rate per second
        self.time_since_last_simulation += Timer.prev_frame_duration
        num_agent_actions: int = self.time_since_last_simulation * agent_acts_per_sec
        self.move_and_look(int(num_agent_actions))

        # Occasionally a frame will pass quicker than 1/60 seconds
        if self.time_since_last_simulation >= 1.0 / self.fps:
            if self.simulating or self.simulate_single_step:
                self.sim.step_world(1.0 / self.fps)
                self.simulate_single_step = False
                if simulation_call is not None:
                    simulation_call()
                # compute object stability after physics step
                self.num_unstable_objects = 0
                for obj_initial_state, obj in zip(
                    self.clutter_object_initial_states, self.clutter_object_instances
                ):
                    translation_error = (
                        obj_initial_state[0] - obj.translation
                    ).length()
                    if translation_error > 0.1:
                        self.num_unstable_objects += 1

            if global_call is not None:
                global_call()

            # reset time_since_last_simulation, accounting for potential overflow
            self.time_since_last_simulation = math.fmod(
                self.time_since_last_simulation, 1.0 / self.fps
            )

        keys = active_agent_id_and_sensor_name

        if self.enable_batch_renderer:
            self.render_batch()
        else:
            self.sim._Simulator__sensors[keys[0]][keys[1]].draw_observation()
            agent = self.sim.get_agent(keys[0])
            self.render_camera = agent.scene_node.node_sensor_suite.get(keys[1])
            self.debug_draw()
            self.render_camera.render_target.blit_rgba_to_default()

        # draw CPU/GPU usage data and other info to the app window
        mn.gl.default_framebuffer.bind()
        self.draw_text(self.render_camera.specification())

        self.swap_buffers()
        Timer.next_frame()
        self.redraw()

    def default_agent_config(self) -> habitat_sim.agent.AgentConfiguration:
        """
        Set up our own agent and agent controls
        """
        make_action_spec = habitat_sim.agent.ActionSpec
        make_actuation_spec = habitat_sim.agent.ActuationSpec
        MOVE, LOOK = 0.07, 1.5

        # all of our possible actions' names
        action_list = [
            "move_left",
            "turn_left",
            "move_right",
            "turn_right",
            "move_backward",
            "look_up",
            "move_forward",
            "look_down",
            "move_down",
            "move_up",
        ]

        action_space: Dict[str, habitat_sim.agent.ActionSpec] = {}

        # build our action space map
        for action in action_list:
            actuation_spec_amt = MOVE if "move" in action else LOOK
            action_spec = make_action_spec(
                action, make_actuation_spec(actuation_spec_amt)
            )
            action_space[action] = action_spec

        sensor_spec: List[habitat_sim.sensor.SensorSpec] = self.cfg.agents[
            self.agent_id
        ].sensor_specifications

        agent_config = habitat_sim.agent.AgentConfiguration(
            height=1.5,
            radius=0.1,
            sensor_specifications=sensor_spec,
            action_space=action_space,
            body_type="cylinder",
        )
        return agent_config

    def reconfigure_sim(
        self, mm: Optional[habitat_sim.metadata.MetadataMediator] = None
    ) -> None:
        """
        Utilizes the current `self.sim_settings` to configure and set up a new
        `habitat_sim.Simulator`, and then either starts a simulation instance, or replaces
        the current simulator instance, reloading the most recently loaded scene
        """
        # configure our sim_settings but then set the agent to our default
        self.cfg = make_cfg(self.sim_settings)
        self.cfg.metadata_mediator = mm
        self.agent_id: int = self.sim_settings["default_agent"]
        self.cfg.agents[self.agent_id] = self.default_agent_config()

        if self.enable_batch_renderer:
            self.cfg.enable_batch_renderer = True
            self.cfg.sim_cfg.create_renderer = False
            self.cfg.sim_cfg.enable_gfx_replay_save = True

        if self.sim_settings["use_default_lighting"]:
            logger.info("Setting default lighting override for scene.")
            self.cfg.sim_cfg.override_scene_light_defaults = True
            self.cfg.sim_cfg.scene_light_setup = habitat_sim.gfx.DEFAULT_LIGHTING_KEY

        if self.sim is None:
            self.tiled_sims = []
            for _i in range(self.num_env):
                self.tiled_sims.append(habitat_sim.Simulator(self.cfg))
            self.sim = self.tiled_sims[0]
        else:  # edge case
            for i in range(self.num_env):
                if (
                    self.tiled_sims[i].config.sim_cfg.scene_id
                    == self.cfg.sim_cfg.scene_id
                ):
                    # we need to force a reset, so change the internal config scene name
                    self.tiled_sims[i].config.sim_cfg.scene_id = "NONE"
                self.tiled_sims[i].reconfigure(self.cfg)

        # post reconfigure
        self.default_agent = self.sim.get_agent(self.agent_id)
        self.render_camera = self.default_agent.scene_node.node_sensor_suite.get(
            "color_sensor"
        )

        # set sim_settings scene name as actual loaded scene
        self.sim_settings["scene"] = self.sim.curr_scene_name

        # Initialize replay renderer
        if self.enable_batch_renderer and self.replay_renderer is None:
            self.replay_renderer_cfg = ReplayRendererConfiguration()
            self.replay_renderer_cfg.num_environments = self.num_env
            self.replay_renderer_cfg.standalone = (
                False  # Context is owned by the GLFW window
            )
            self.replay_renderer_cfg.sensor_specifications = self.cfg.agents[
                self.agent_id
            ].sensor_specifications
            self.replay_renderer_cfg.gpu_device_id = self.cfg.sim_cfg.gpu_device_id
            self.replay_renderer_cfg.force_separate_semantic_scene_graph = False
            self.replay_renderer_cfg.leave_context_with_background_renderer = False
            self.replay_renderer = ReplayRenderer.create_batch_replay_renderer(
                self.replay_renderer_cfg
            )
            # Pre-load composite files
            if sim_settings["composite_files"] is not None:
                for composite_file in sim_settings["composite_files"]:
                    self.replay_renderer.preload_file(composite_file)

        self.ao_link_map = sutils.get_ao_link_id_map(self.sim)

        self.marker_sets = sutils.parse_scene_marker_sets(self.sim)

        Timer.start()
        self.step = -1

    def render_batch(self):
        """
        This method updates the replay manager with the current state of environments and renders them.
        """
        for i in range(self.num_env):
            # Apply keyframe
            keyframe = self.tiled_sims[i].gfx_replay_manager.extract_keyframe()
            self.replay_renderer.set_environment_keyframe(i, keyframe)
            # Copy sensor transforms
            sensor_suite = self.tiled_sims[i]._sensors
            for sensor_uuid, sensor in sensor_suite.items():
                transform = sensor._sensor_object.node.absolute_transformation()
                self.replay_renderer.set_sensor_transform(i, sensor_uuid, transform)
        # Render
        self.replay_renderer.render(mn.gl.default_framebuffer)

    def move_and_look(self, repetitions: int) -> None:
        """
        This method is called continuously with `self.draw_event` to monitor
        any changes in the movement keys map `Dict[KeyEvent.key, Bool]`.
        When a key in the map is set to `True` the corresponding action is taken.
        """
        # avoids unnecessary updates to grabber's object position
        if repetitions == 0:
            return

        key = Application.KeyEvent.Key
        agent = self.sim.agents[self.agent_id]
        press: Dict[key.key, bool] = self.pressed
        act: Dict[key.key, str] = self.key_to_action

        action_queue: List[str] = [act[k] for k, v in press.items() if v]

        for _ in range(int(repetitions)):
            [agent.act(x) for x in action_queue]

        # update the grabber transform when our agent is moved
        if self.mouse_grabber is not None:
            # update location of grabbed object
            self.update_grab_position(self.previous_mouse_point)

    def invert_gravity(self) -> None:
        """
        Sets the gravity vector to the negative of it's previous value. This is
        a good method for testing simulation functionality.
        """
        gravity: mn.Vector3 = self.sim.get_gravity() * -1
        self.sim.set_gravity(gravity)

    def key_press_event(self, event: Application.KeyEvent) -> None:
        """
        Handles `Application.KeyEvent` on a key press by performing the corresponding functions.
        If the key pressed is part of the movement keys map `Dict[KeyEvent.key, Bool]`, then the
        key will be set to False for the next `self.move_and_look()` to update the current actions.
        """
        key = event.key
        pressed = Application.KeyEvent.Key
        mod = Application.InputEvent.Modifier

        shift_pressed = bool(event.modifiers & mod.SHIFT)
        alt_pressed = bool(event.modifiers & mod.ALT)
        # warning: ctrl doesn't always pass through with other key-presses

        if key == pressed.ESC:
            event.accepted = True
            self.exit_event(Application.ExitEvent)
            return

        elif key == pressed.H:
            self.print_help_text()
        elif key == pressed.J:
            logger.info(
                f"Toggle Region Draw from {self.semantic_region_debug_draw } to {not self.semantic_region_debug_draw}"
            )
            # Toggle visualize semantic bboxes. Currently only regions supported
            self.semantic_region_debug_draw = not self.semantic_region_debug_draw

        elif key == pressed.TAB:
            # NOTE: (+ALT) - reconfigure without cycling scenes
            if not alt_pressed:
                # cycle the active scene from the set available in MetadataMediator
                inc = -1 if shift_pressed else 1
                scene_ids = self.sim.metadata_mediator.get_scene_handles()
                cur_scene_index = 0
                if self.sim_settings["scene"] not in scene_ids:
                    matching_scenes = [
                        (ix, x)
                        for ix, x in enumerate(scene_ids)
                        if self.sim_settings["scene"] in x
                    ]
                    if not matching_scenes:
                        logger.warning(
                            f"The current scene, '{self.sim_settings['scene']}', is not in the list, starting cycle at index 0."
                        )
                    else:
                        cur_scene_index = matching_scenes[0][0]
                else:
                    cur_scene_index = scene_ids.index(self.sim_settings["scene"])

                next_scene_index = min(
                    max(cur_scene_index + inc, 0), len(scene_ids) - 1
                )
                self.sim_settings["scene"] = scene_ids[next_scene_index]
            self.reconfigure_sim()
            logger.info(
                f"Reconfigured simulator for scene: {self.sim_settings['scene']}"
            )

        elif key == pressed.SPACE:
            if not self.sim.config.sim_cfg.enable_physics:
                logger.warn("Warning: physics was not enabled during setup")
            else:
                self.simulating = not self.simulating
                logger.info(f"Command: physics simulating set to {self.simulating}")

        elif key == pressed.PERIOD:
            if self.simulating:
                logger.warn("Warning: physics simulation already running")
            else:
                self.simulate_single_step = True
                logger.info("Command: physics step taken")

        elif key == pressed.COMMA:
            self.debug_bullet_draw = not self.debug_bullet_draw
            logger.info(f"Command: toggle Bullet debug draw: {self.debug_bullet_draw}")

        elif key == pressed.C:
            if shift_pressed:
                self.contact_debug_draw = not self.contact_debug_draw
                logger.info(
                    f"Command: toggle contact debug draw: {self.contact_debug_draw}"
                )
            else:
                # perform a discrete collision detection pass and enable contact debug drawing to visualize the results
                logger.info(
                    "Command: perform discrete collision detection and visualize active contacts."
                )
                self.sim.perform_discrete_collision_detection()
                self.contact_debug_draw = True
                # TODO: add a nice log message with concise contact pair naming.

        elif key == pressed.T:
            self.modify_param_from_term()

            # load URDF
            # fixed_base = alt_pressed
            # urdf_file_path = ""
            # if shift_pressed and self.cached_urdf:
            #     urdf_file_path = self.cached_urdf
            # else:
            #     urdf_file_path = input("Load URDF: provide a URDF filepath:").strip()
            # if not urdf_file_path:
            #     logger.warn("Load URDF: no input provided. Aborting.")
            # elif not urdf_file_path.endswith((".URDF", ".urdf")):
            #     logger.warn("Load URDF: input is not a URDF. Aborting.")
            # elif os.path.exists(urdf_file_path):
            #     self.cached_urdf = urdf_file_path
            #     aom = self.sim.get_articulated_object_manager()
            #     ao = aom.add_articulated_object_from_urdf(
            #         urdf_file_path,
            #         fixed_base,
            #         1.0,
            #         1.0,
            #         True,
            #         maintain_link_order=False,
            #         intertia_from_urdf=False,
            #     )
            #     ao.translation = (
            #         self.default_agent.scene_node.transformation.transform_point(
            #             [0.0, 1.0, -1.5]
            #         )
            #     )
            #     # check removal and auto-creation
            #     joint_motor_settings = habitat_sim.physics.JointMotorSettings(
            #         position_target=0.0,
            #         position_gain=1.0,
            #         velocity_target=0.0,
            #         velocity_gain=1.0,
            #         max_impulse=1000.0,
            #     )
            #     existing_motor_ids = ao.existing_joint_motor_ids
            #     for motor_id in existing_motor_ids:
            #         ao.remove_joint_motor(motor_id)
            #     ao.create_all_motors(joint_motor_settings)
            # else:
            #     logger.warn("Load URDF: input file not found. Aborting.")

        elif key == pressed.M:
            if (
                shift_pressed
                and self.selected_object is not None
                and isinstance(self.selected_object, physics.ManagedArticulatedObject)
                and self.selected_object.handle in self.marker_sets
            ):
                sutils.write_ao_marker_sets(
                    self.selected_object, self.marker_sets[self.selected_object.handle]
                )
                sutils.write_ao_marker_set_to_template(self.sim, self.selected_object)
                print(f"Saved config for {self.selected_object.handle}.")
            else:
                self.cycle_mouse_mode()
                logger.info(f"Command: mouse mode set to {self.mouse_interaction}")

        elif key == pressed.V:
            # load receptacles and toggle visibilty or color mode (+SHIFT)
            if self.receptacles is None:
                self.load_receptacles()

            if shift_pressed:
                self.rec_color_mode = RecColorMode(
                    (self.rec_color_mode.value + 1) % len(RecColorMode)
                )
                print(f"self.rec_color_mode = {self.rec_color_mode}")
                self.display_receptacles = True
            else:
                self.display_receptacles = not self.display_receptacles
                print(f"self.display_receptacles = {self.display_receptacles}")

        elif key == pressed.F:
            # toggle, load(+ALT), or save(+SHIFT) filtering
            if shift_pressed and self.rec_filter_data is not None:
                self.export_filtered_recs(self.rec_filter_path)
            elif alt_pressed:
                self.load_filtered_recs(self.rec_filter_path)
            else:
                self.show_filtered = not self.show_filtered
                print(f"self.show_filtered = {self.show_filtered}")

        elif key == pressed.U:
            rom = self.sim.get_rigid_object_manager()
            # add objects to the selected receptacle or remove al objects
            if shift_pressed:
                # remove all
                print(f"Removing {len(self.clutter_object_instances)} clutter objects.")
                for obj in self.clutter_object_instances:
                    rom.remove_object_by_handle(obj.handle)
                self.clutter_object_initial_states.clear()
                self.clutter_object_instances.clear()
            else:
                # try to sample an object from the selected object receptacles
                rec_set = None
                if alt_pressed:
                    # use all active filter recs
                    rec_set = [
                        rec
                        for rec in self.receptacles
                        if rec.unique_name in self.rec_filter_data["active"]
                    ]
                elif self.selected_rec is not None:
                    rec_set = [self.selected_rec]
                elif self.selected_object is not None:
                    rec_set = [
                        rec
                        for rec in self.receptacles
                        if self.selected_object.handle == rec.parent_object_handle
                    ]
                if rec_set is not None:
                    if len(self.clutter_object_handles) == 0:
                        for obj_name in self.clutter_object_set:
                            matching_handles = self.sim.metadata_mediator.object_template_manager.get_template_handles(
                                obj_name
                            )
                            assert (
                                len(matching_handles) > 0
                            ), f"No matching template for '{obj_name}' in the dataset."
                            self.clutter_object_handles.append(matching_handles[0])

                    rec_set_unique_names = [rec.unique_name for rec in rec_set]
                    obj_samp = ObjectSampler(
                        self.clutter_object_handles,
                        ["rec set"],
                        orientation_sample="up",
                        num_objects=(1, 10),
                    )
                    rec_set_obj = hab_receptacle.ReceptacleSet(
                        "rec set", [""], [], rec_set_unique_names, []
                    )
                    obj_count_dict = {rec.unique_name: 200 for rec in rec_set}
                    recep_tracker = hab_receptacle.ReceptacleTracker(
                        obj_count_dict,
                        {"rec set": rec_set_obj},
                    )
                    new_objs = obj_samp.sample(
                        self.sim, recep_tracker, [], snap_down=True
                    )
                    for obj, rec in new_objs:
                        self.clutter_object_instances.append(obj)
                        self.clutter_object_initial_states.append(
                            (obj.translation, obj.rotation)
                        )
                        print(f"Sampled '{obj.handle}' in '{rec.unique_name}'")
                else:
                    print("No object selected, cannot sample clutter.")

        elif key == pressed.O:
            if shift_pressed:
                # move non-proxy objects in/out of visible space
                self.original_objs_visible = not self.original_objs_visible
                print(f"self.original_objs_visible = {self.original_objs_visible}")
                if not self.original_objs_visible:
                    for _obj_handle, obj in (
                        self.sim.get_rigid_object_manager()
                        .get_objects_by_handle_substring()
                        .items()
                    ):
                        if self.proxy_obj_postfix not in obj.creation_attributes.handle:
                            obj.motion_type = habitat_sim.physics.MotionType.KINEMATIC
                            obj.translation = obj.translation + mn.Vector3(200, 0, 0)
                            obj.motion_type = habitat_sim.physics.MotionType.STATIC
                else:
                    for _obj_handle, obj in (
                        self.sim.get_rigid_object_manager()
                        .get_objects_by_handle_substring()
                        .items()
                    ):
                        if self.proxy_obj_postfix not in obj.creation_attributes.handle:
                            obj.motion_type = habitat_sim.physics.MotionType.KINEMATIC
                            obj.translation = obj.translation - mn.Vector3(200, 0, 0)
                            obj.motion_type = habitat_sim.physics.MotionType.STATIC

            else:
                if self.col_proxy_objs is None:
                    self.col_proxy_objs = []
                    for _obj_handle, obj in (
                        self.sim.get_rigid_object_manager()
                        .get_objects_by_handle_substring()
                        .items()
                    ):
                        if self.proxy_obj_postfix not in obj.creation_attributes.handle:
                            # add a new proxy object
                            self.col_proxy_objs.append(self.add_col_proxy_object(obj))
                else:
                    self.col_proxies_visible = not self.col_proxies_visible
                    print(f"self.col_proxies_visible = {self.col_proxies_visible}")

                    # make the proxies visible or not by moving them
                    if not self.col_proxies_visible:
                        for obj in self.col_proxy_objs:
                            obj.translation = obj.translation + mn.Vector3(200, 0, 0)
                    else:
                        for obj in self.col_proxy_objs:
                            obj.translation = obj.translation - mn.Vector3(200, 0, 0)

        elif key == pressed.N:
            # (default) - toggle navmesh visualization
            # NOTE: (+ALT) - re-sample the agent position on the NavMesh
            # NOTE: (+SHIFT) - re-compute the NavMesh
            if alt_pressed:
                logger.info("Command: resample agent state from navmesh")
                if self.sim.pathfinder.is_loaded:
                    new_agent_state = habitat_sim.AgentState()
                    largest_island_ix = get_largest_island_index(
                        pathfinder=self.sim.pathfinder,
                        sim=self.sim,
                        allow_outdoor=False,
                    )
                    print(f"Largest indoor island index = {largest_island_ix}")
                    new_agent_state.position = (
                        self.sim.pathfinder.get_random_navigable_point(
                            island_index=largest_island_ix
                        )
                    )
                    new_agent_state.rotation = quat_from_angle_axis(
                        self.sim.random.uniform_float(0, 2.0 * np.pi),
                        np.array([0, 1, 0]),
                    )
                    self.default_agent.set_state(new_agent_state)
                else:
                    logger.warning(
                        "NavMesh is not initialized. Cannot sample new agent state."
                    )
            elif shift_pressed:
                logger.info("Command: recompute navmesh")
                self.navmesh_config_and_recompute()
            else:
                if self.sim.pathfinder.is_loaded:
                    self.sim.navmesh_visualization = not self.sim.navmesh_visualization
                    logger.info("Command: toggle navmesh")
                else:
                    logger.warn("Warning: recompute navmesh first")

        # update map of moving/looking keys which are currently pressed
        if key in self.pressed:
            self.pressed[key] = True
        event.accepted = True
        self.redraw()

    def key_release_event(self, event: Application.KeyEvent) -> None:
        """
        Handles `Application.KeyEvent` on a key release. When a key is released, if it
        is part of the movement keys map `Dict[KeyEvent.key, Bool]`, then the key will
        be set to False for the next `self.move_and_look()` to update the current actions.
        """
        key = event.key

        # update map of moving/looking keys which are currently pressed
        if key in self.pressed:
            self.pressed[key] = False
        event.accepted = True
        self.redraw()

    def mouse_move_event(self, event: Application.MouseMoveEvent) -> None:
        """
        Handles `Application.MouseMoveEvent`. When in LOOK mode, enables the left
        mouse button to steer the agent's facing direction. When in GRAB mode,
        continues to update the grabber's object position with our agents position.
        """

        render_camera = self.render_camera.render_camera
        ray = render_camera.unproject(self.get_mouse_position(event.position))
        self.mouse_cast_results = self.sim.cast_ray(ray=ray)

        button = Application.MouseMoveEvent.Buttons
        # if interactive mode -> LOOK MODE
        if event.buttons == button.LEFT and self.mouse_interaction == MouseMode.LOOK:
            agent = self.sim.agents[self.agent_id]
            delta = self.get_mouse_position(event.relative_position) / 2
            action = habitat_sim.agent.ObjectControls()
            act_spec = habitat_sim.agent.ActuationSpec

            # left/right on agent scene node
            action(agent.scene_node, "turn_right", act_spec(delta.x))

            # up/down on cameras' scene nodes
            action = habitat_sim.agent.ObjectControls()
            sensors = list(self.default_agent.scene_node.subtree_sensors.values())
            [action(s.object, "look_down", act_spec(delta.y), False) for s in sensors]

        # if interactive mode is TRUE -> GRAB MODE
        elif self.mouse_interaction == MouseMode.GRAB and self.mouse_grabber:
            # update location of grabbed object
            self.update_grab_position(self.get_mouse_position(event.position))

        self.previous_mouse_point = self.get_mouse_position(event.position)
        self.redraw()
        event.accepted = True

    def mouse_press_event(self, event: Application.MouseEvent) -> None:
        """
        Handles `Application.MouseEvent`. When in GRAB mode, click on
        objects to drag their position. (right-click for fixed constraints)
        """
        button = Application.MouseEvent.Button
        physics_enabled = self.sim.get_physics_simulation_library()
        mod = Application.InputEvent.Modifier
        shift_pressed = bool(event.modifiers & mod.SHIFT)
        alt_pressed = bool(event.modifiers & mod.ALT)

        # if interactive mode is True -> GRAB MODE
        if self.mouse_interaction == MouseMode.GRAB and physics_enabled:
            ao_link = -1
            hit_info = self.mouse_cast_results.hits[0]

            if hit_info.object_id > habitat_sim.stage_id:
                obj = sutils.get_obj_from_id(
                    self.sim, hit_info.object_id, self.ao_link_map
                )

                if obj is None:
                    raise AssertionError(
                        "hit object_id is not valid. Did not find object or link."
                    )

                if obj.object_id == hit_info.object_id:
                    # ro or ao base
                    object_pivot = obj.transformation.inverted().transform_point(
                        hit_info.point
                    )
                    object_frame = obj.rotation.inverted()
                elif isinstance(obj, physics.ManagedArticulatedObject):
                    # link
                    ao_link = obj.link_object_ids[hit_info.object_id]
                    object_pivot = (
                        obj.get_link_scene_node(ao_link)
                        .transformation.inverted()
                        .transform_point(hit_info.point)
                    )
                    object_frame = obj.get_link_scene_node(ao_link).rotation.inverted()

                print(f"Grabbed object {obj.handle}")
                if ao_link >= 0:
                    print(f"    link id {ao_link}")

                # setup the grabbing constraints
                node = self.default_agent.scene_node
                constraint_settings = physics.RigidConstraintSettings()

                constraint_settings.object_id_a = obj.object_id
                constraint_settings.link_id_a = ao_link
                constraint_settings.pivot_a = object_pivot
                constraint_settings.frame_a = (
                    object_frame.to_matrix() @ node.rotation.to_matrix()
                )
                constraint_settings.frame_b = node.rotation.to_matrix()
                constraint_settings.pivot_b = hit_info.point

                # by default use a point 2 point constraint
                if event.button == button.RIGHT:
                    constraint_settings.constraint_type = (
                        physics.RigidConstraintType.Fixed
                    )

                grip_depth = (
                    hit_info.point
                    - self.render_camera.render_camera.node.absolute_translation
                ).length()

                self.mouse_grabber = MouseGrabber(
                    constraint_settings,
                    grip_depth,
                    self.sim,
                )

                # end if didn't hit the scene
            # end has raycast hit
        # end has physics enabled

        elif (
            self.mouse_interaction == MouseMode.LOOK
            and physics_enabled
            and self.mouse_cast_results is not None
            and self.mouse_cast_results.has_hits()
            and event.button == button.RIGHT
        ):
            self.selected_object = None
            self.selected_rec = None
            hit_id = self.mouse_cast_results.hits[0].object_id
            # right click in look mode to print object information
            if hit_id == habitat_sim.stage_id:
                print("This is the stage.")
            else:
                obj = sutils.get_obj_from_id(self.sim, hit_id)
                self.selected_object = obj
                print(f"Object: {obj.handle}")
                if self.receptacles is not None:
                    for rec in self.receptacles:
                        if rec.parent_object_handle == obj.handle:
                            print(f"    - Receptacle: {rec.name}")
                if shift_pressed:
                    self.selected_rec = self.get_closest_tri_receptacle(
                        self.mouse_cast_results.hits[0].point
                    )
                    if self.selected_rec is not None:
                        print(f"Selected Receptacle: {self.selected_rec.name}")
                elif alt_pressed:
                    filtered_rec = self.get_closest_tri_receptacle(
                        self.mouse_cast_results.hits[0].point
                    )
                    if filtered_rec is not None:
                        filtered_rec_name = self.get_rec_instance_name(filtered_rec)
                        print(f"Modified Receptacle Filter State: {filtered_rec_name}")
                        if (
                            filtered_rec_name
                            in self.rec_filter_data["manually_filtered"]
                        ):
                            print(" remove from manual filter")
                            # this was manually filtered, remove it and try to make active
                            self.rec_filter_data["manually_filtered"].remove(
                                filtered_rec_name
                            )
                            add_to_active = True
                            for other_out_set in [
                                "access_filtered",
                                "stability_filtered",
                                "height_filtered",
                            ]:
                                if (
                                    filtered_rec_name
                                    in self.rec_filter_data[other_out_set]
                                ):
                                    print(f"     is in {other_out_set}")
                                    add_to_active = False
                                    break
                            if add_to_active:
                                print("     is active")
                                self.rec_filter_data["active"].append(filtered_rec_name)
                        elif filtered_rec_name in self.rec_filter_data["active"]:
                            print(" remove from active, add manual filter")
                            # this was active, remove it and mark manually filtered
                            self.rec_filter_data["active"].remove(filtered_rec_name)
                            self.rec_filter_data["manually_filtered"].append(
                                filtered_rec_name
                            )
                        else:
                            print(" add to manual filter, but has other filter")
                            # this is already filtered, but add it to manual filters
                            self.rec_filter_data["manually_filtered"].append(
                                filtered_rec_name
                            )
        elif (
            self.mouse_interaction == MouseMode.MARKER
            and physics_enabled
            and self.mouse_cast_results is not None
            and self.mouse_cast_results.has_hits()
        ):
            hit_info = self.mouse_cast_results.hits[0]
            obj = sutils.get_obj_from_id(self.sim, hit_info.object_id, self.ao_link_map)

            if (
                isinstance(obj, physics.ManagedArticulatedObject)
                and obj.object_id != hit_info.object_id
            ):
                # this is an ArticulatedLink, so we can add markers'
                link_ix = obj.link_object_ids[hit_info.object_id]
                link_node = obj.get_link_scene_node(link_ix)
                local_hit_point = link_node.transformation.inverted().transform_point(
                    hit_info.point
                )
                if self.marker_sets is not None:
                    if obj.handle not in self.marker_sets:
                        self.marker_sets[obj.handle] = {}
                    if link_ix not in self.marker_sets[obj.handle]:
                        self.marker_sets[obj.handle][link_ix] = {}
                    existing_set_names = list(
                        self.marker_sets[obj.handle][link_ix].keys()
                    )
                    selected_set_name = None
                    if self.selected_marker_set_index >= len(existing_set_names):
                        self.selected_marker_set_index = len(existing_set_names)
                        selected_set_name = f"handle_{self.selected_marker_set_index}"
                    else:
                        selected_set_name = existing_set_names[
                            self.selected_marker_set_index
                        ]
                    if (
                        event.button == button.RIGHT
                        and selected_set_name in existing_set_names
                    ):
                        # remove instead of add
                        closest_marker_index = None
                        closest_marker_dist = 9999
                        for m_ix in range(
                            len(
                                self.marker_sets[obj.handle][link_ix][selected_set_name]
                            )
                        ):
                            m_dist = (
                                local_hit_point
                                - self.marker_sets[obj.handle][link_ix][
                                    selected_set_name
                                ][m_ix]
                            ).length()
                            if m_dist < closest_marker_dist:
                                closest_marker_dist = m_dist
                                closest_marker_index = m_ix
                        if closest_marker_index is not None:
                            del self.marker_sets[obj.handle][link_ix][
                                selected_set_name
                            ][closest_marker_index]
                    elif event.button == button.LEFT:
                        # add a point
                        if (
                            selected_set_name
                            not in self.marker_sets[obj.handle][link_ix]
                        ):
                            self.marker_sets[obj.handle][link_ix][
                                selected_set_name
                            ] = []
                        self.marker_sets[obj.handle][link_ix][selected_set_name].append(
                            local_hit_point
                        )
            else:
                obj_name = "stage"
                if obj is not None:
                    obj_name = obj.handle
                print(
                    f"Can't add marker, hit non link object ({hit_info.object_id}): '{obj_name}'."
                )

        self.previous_mouse_point = self.get_mouse_position(event.position)
        self.redraw()
        event.accepted = True

    def mouse_scroll_event(self, event: Application.MouseScrollEvent) -> None:
        """
        Handles `Application.MouseScrollEvent`. When in LOOK mode, enables camera
        zooming (fine-grained zoom using shift) When in GRAB mode, adjusts the depth
        of the grabber's object. (larger depth change rate using shift)
        """
        scroll_mod_val = (
            event.offset.y
            if abs(event.offset.y) > abs(event.offset.x)
            else event.offset.x
        )
        if not scroll_mod_val:
            return

        # use shift to scale action response
        shift_pressed = bool(event.modifiers & Application.InputEvent.Modifier.SHIFT)
        alt_pressed = bool(event.modifiers & Application.InputEvent.Modifier.ALT)
        ctrl_pressed = bool(event.modifiers & Application.InputEvent.Modifier.CTRL)

        # if interactive mode is False -> LOOK MODE
        if self.mouse_interaction == MouseMode.LOOK:
            # use shift for fine-grained zooming
            mod_val = 1.01 if shift_pressed else 1.1
            mod = mod_val if scroll_mod_val > 0 else 1.0 / mod_val
            cam = self.render_camera
            cam.zoom(mod)
            self.redraw()

        elif self.mouse_interaction == MouseMode.GRAB and self.mouse_grabber:
            # adjust the depth
            mod_val = 0.1 if shift_pressed else 0.01
            scroll_delta = scroll_mod_val * mod_val
            if alt_pressed or ctrl_pressed:
                # rotate the object's local constraint frame
                agent_t = self.default_agent.scene_node.transformation_matrix()
                # ALT - yaw
                rotation_axis = agent_t.transform_vector(mn.Vector3(0, 1, 0))
                if alt_pressed and ctrl_pressed:
                    # ALT+CTRL - roll
                    rotation_axis = agent_t.transform_vector(mn.Vector3(0, 0, -1))
                elif ctrl_pressed:
                    # CTRL - pitch
                    rotation_axis = agent_t.transform_vector(mn.Vector3(1, 0, 0))
                self.mouse_grabber.rotate_local_frame_by_global_angle_axis(
                    rotation_axis, mn.Rad(scroll_delta)
                )
            else:
                # update location of grabbed object
                self.mouse_grabber.grip_depth += scroll_delta
                self.update_grab_position(self.get_mouse_position(event.position))
        elif self.mouse_interaction == MouseMode.MARKER:
            if scroll_mod_val > 0:
                self.selected_marker_set_index += 1
            else:
                self.selected_marker_set_index = max(
                    self.selected_marker_set_index - 1, 0
                )

        self.redraw()
        event.accepted = True

    def mouse_release_event(self, event: Application.MouseEvent) -> None:
        """
        Release any existing constraints.
        """
        del self.mouse_grabber
        self.mouse_grabber = None
        event.accepted = True

    def update_grab_position(self, point: mn.Vector2i) -> None:
        """
        Accepts a point derived from a mouse click event and updates the
        transform of the mouse grabber.
        """
        # check mouse grabber
        if not self.mouse_grabber:
            return

        render_camera = self.render_camera.render_camera
        ray = render_camera.unproject(point)

        rotation: mn.Matrix3x3 = self.default_agent.scene_node.rotation.to_matrix()
        translation: mn.Vector3 = (
            render_camera.node.absolute_translation
            + ray.direction * self.mouse_grabber.grip_depth
        )
        self.mouse_grabber.update_transform(mn.Matrix4.from_(rotation, translation))

    def get_mouse_position(self, mouse_event_position: mn.Vector2i) -> mn.Vector2i:
        """
        This function will get a screen-space mouse position appropriately
        scaled based on framebuffer size and window size.  Generally these would be
        the same value, but on certain HiDPI displays (Retina displays) they may be
        different.
        """
        scaling = mn.Vector2i(self.framebuffer_size) / mn.Vector2i(self.window_size)
        return mouse_event_position * scaling

    def cycle_mouse_mode(self) -> None:
        """
        This method defines how to cycle through the mouse mode.
        """
        if self.mouse_interaction == MouseMode.LOOK:
            self.mouse_interaction = MouseMode.GRAB
        elif self.mouse_interaction == MouseMode.GRAB:
            self.mouse_interaction = MouseMode.MARKER
        elif self.mouse_interaction == MouseMode.MARKER:
            self.mouse_interaction = MouseMode.LOOK

    def navmesh_config_and_recompute(self) -> None:
        """
        This method is setup to be overridden in for setting config accessibility
        in inherited classes.
        """
        self.navmesh_settings = habitat_sim.NavMeshSettings()
        self.navmesh_settings.set_defaults()
        self.navmesh_settings.agent_height = self.cfg.agents[self.agent_id].height
        self.navmesh_settings.agent_radius = self.cfg.agents[self.agent_id].radius
        self.navmesh_settings.include_static_objects = True
        self.sim.recompute_navmesh(
            self.sim.pathfinder,
            self.navmesh_settings,
        )

    def exit_event(self, event: Application.ExitEvent):
        """
        Overrides exit_event to properly close the Simulator before exiting the
        application.
        """
        for i in range(self.num_env):
            self.tiled_sims[i].close(destroy=True)
            event.accepted = True
        exit(0)

    def draw_text(self, sensor_spec):
        # make magnum text background transparent for text
        mn.gl.Renderer.enable(mn.gl.Renderer.Feature.BLENDING)
        mn.gl.Renderer.set_blend_function(
            mn.gl.Renderer.BlendFunction.ONE,
            mn.gl.Renderer.BlendFunction.ONE_MINUS_SOURCE_ALPHA,
        )

        self.shader.bind_vector_texture(self.glyph_cache.texture)
        self.shader.transformation_projection_matrix = self.window_text_transform
        self.shader.color = [1.0, 1.0, 1.0]

        sensor_type_string = str(sensor_spec.sensor_type.name)
        sensor_subtype_string = str(sensor_spec.sensor_subtype.name)
        if self.mouse_interaction == MouseMode.LOOK:
            mouse_mode_string = "LOOK"
        elif self.mouse_interaction == MouseMode.GRAB:
            mouse_mode_string = "GRAB"
        elif self.mouse_interaction == MouseMode.MARKER:
            mouse_mode_string = "MARKER"
        self.window_text.render(
            f"""
{self.fps} FPS
Sensor Type: {sensor_type_string}
Sensor Subtype: {sensor_subtype_string}
Mouse Interaction Mode: {mouse_mode_string}
Selected marker_set index: {self.selected_marker_set_index}
Unstable Objects: {self.num_unstable_objects} of {len(self.clutter_object_instances)}
            """
        )
        self.shader.draw(self.window_text.mesh)

        # Disable blending for text
        mn.gl.Renderer.disable(mn.gl.Renderer.Feature.BLENDING)

    def print_help_text(self) -> None:
        """
        Print the Key Command help text.
        """
        logger.info(
            """
=====================================================
Welcome to the Habitat-sim Python Viewer application!
=====================================================
Mouse Functions ('m' to toggle mode):
----------------
In LOOK mode (default):
    LEFT:
        Click and drag to rotate the agent and look up/down.
    WHEEL:
        Modify orthographic camera zoom/perspective camera FOV (+SHIFT for fine grained control)
    RIGHT:
        Click an object to select the object. Prints object name and attached receptacle names. Selected object displays sample points when cpo is initialized.
        (+SHIFT) select a receptacle.
        (+ALT) add or remove a receptacle from the "manual filter set".
In GRAB mode (with 'enable-physics'):
    LEFT:
        Click and drag to pickup and move an object with a point-to-point constraint (e.g. ball joint).
    RIGHT:
        Click and drag to pickup and move an object with a fixed frame constraint.
    WHEEL (with picked object):
        default - Pull gripped object closer or push it away.
        (+ALT) rotate object fixed constraint frame (yaw)
        (+CTRL) rotate object fixed constraint frame (pitch)
        (+ALT+CTRL) rotate object fixed constraint frame (roll)
        (+SHIFT) amplify scroll magnitude


Key Commands:
-------------
    esc:        Exit the application.
    'h':        Display this help message.
    'm':        Cycle mouse interaction modes.

    Agent Controls:
    'wasd':     Move the agent's body forward/backward and left/right.
    'zx':       Move the agent's body up/down.
    arrow keys: Turn the agent's body left/right and camera look up/down.

    Utilities:
    'r':        Reset the simulator with the most recently loaded scene.
    'n':        Show/hide NavMesh wireframe.
                (+SHIFT) Recompute NavMesh with default settings.
                (+ALT) Re-sample the agent(camera)'s position and orientation from the NavMesh.
    ',':        Render a Bullet collision shape debug wireframe overlay (white=active, green=sleeping, blue=wants sleeping, red=can't sleep).
    'c':        Run a discrete collision detection pass and render a debug wireframe overlay showing active contact points and normals (yellow=fixed length normals, red=collision distances).
                (+SHIFT) Toggle the contact point debug render overlay on/off.
    'j'         Toggle Semantic visualization bounds (currently only Semantic Region annotations)

    Object Interactions:
    SPACE:      Toggle physics simulation on/off.
    '.':        Take a single simulation step if not simulating continuously.

    Receptacle Evaluation Tool UI:
    'v':        Load all Receptacles for the scene and toggle Receptacle visibility.
                (+SHIFT) Iterate through receptacle color modes.
    'f':        Toggle Receptacle view filtering. When on, only non-filtered Receptacles are visible.
                (+SHIFT) Export current filter metadata to file.
                (+ALT) Import filter metadata from file.
    'o':        Toggle display of collision proxy shapes for the scene.
                (+SHIFT) Toggle display of original render shapes (and Receptacles).
    't':        CLI for modifying un-bound viewer parameters during runtime.
    'u':        Sample an object placement from the currently selected object or receptacle.
                (+SHIFT) Remove all previously sampled objects.
                (+ALT) Sample from all "active" unfiltered Receptacles.

=====================================================
"""
        )


class MouseMode(Enum):
    LOOK = 0
    GRAB = 1
    MOTION = 2
    MARKER = 3


class MouseGrabber:
    """
    Create a MouseGrabber from RigidConstraintSettings to manipulate objects.
    """

    def __init__(
        self,
        settings: physics.RigidConstraintSettings,
        grip_depth: float,
        sim: habitat_sim.simulator.Simulator,
    ) -> None:
        self.settings = settings
        self.simulator = sim

        # defines distance of the grip point from the camera for pivot updates
        self.grip_depth = grip_depth
        self.constraint_id = sim.create_rigid_constraint(settings)

    def __del__(self):
        self.remove_constraint()

    def remove_constraint(self) -> None:
        """
        Remove a rigid constraint by id.
        """
        self.simulator.remove_rigid_constraint(self.constraint_id)

    def updatePivot(self, pos: mn.Vector3) -> None:
        self.settings.pivot_b = pos
        self.simulator.update_rigid_constraint(self.constraint_id, self.settings)

    def update_frame(self, frame: mn.Matrix3x3) -> None:
        self.settings.frame_b = frame
        self.simulator.update_rigid_constraint(self.constraint_id, self.settings)

    def update_transform(self, transform: mn.Matrix4) -> None:
        self.settings.frame_b = transform.rotation()
        self.settings.pivot_b = transform.translation
        self.simulator.update_rigid_constraint(self.constraint_id, self.settings)

    def rotate_local_frame_by_global_angle_axis(
        self, axis: mn.Vector3, angle: mn.Rad
    ) -> None:
        """rotate the object's local constraint frame with a global angle axis input."""
        object_transform = mn.Matrix4()
        rom = self.simulator.get_rigid_object_manager()
        aom = self.simulator.get_articulated_object_manager()
        if rom.get_library_has_id(self.settings.object_id_a):
            object_transform = rom.get_object_by_id(
                self.settings.object_id_a
            ).transformation
        else:
            # must be an ao
            object_transform = (
                aom.get_object_by_id(self.settings.object_id_a)
                .get_link_scene_node(self.settings.link_id_a)
                .transformation
            )
        local_axis = object_transform.inverted().transform_vector(axis)
        R = mn.Matrix4.rotation(angle, local_axis.normalized())
        self.settings.frame_a = R.rotation().__matmul__(self.settings.frame_a)
        self.simulator.update_rigid_constraint(self.constraint_id, self.settings)


class Timer:
    """
    Timer class used to keep track of time between buffer swaps
    and guide the display frame rate.
    """

    start_time = 0.0
    prev_frame_time = 0.0
    prev_frame_duration = 0.0
    running = False

    @staticmethod
    def start() -> None:
        """
        Starts timer and resets previous frame time to the start time.
        """
        Timer.running = True
        Timer.start_time = time.time()
        Timer.prev_frame_time = Timer.start_time
        Timer.prev_frame_duration = 0.0

    @staticmethod
    def stop() -> None:
        """
        Stops timer and erases any previous time data, resetting the timer.
        """
        Timer.running = False
        Timer.start_time = 0.0
        Timer.prev_frame_time = 0.0
        Timer.prev_frame_duration = 0.0

    @staticmethod
    def next_frame() -> None:
        """
        Records previous frame duration and updates the previous frame timestamp
        to the current time. If the timer is not currently running, perform nothing.
        """
        if not Timer.running:
            return
        Timer.prev_frame_duration = time.time() - Timer.prev_frame_time
        Timer.prev_frame_time = time.time()


def init_cpo_for_scene(sim_settings, mm: habitat_sim.metadata.MetadataMediator):
    """
    Initialize and run th CPO for all objects in the scene.
    """
    global _cpo
    global _cpo_threads

    _cpo = csa.CollisionProxyOptimizer(sim_settings, None, mm)

    # get object handles from a specific scene
    objects_in_scene = csa.get_objects_in_scene(
        dataset_path=sim_settings["scene_dataset_config_file"],
        scene_handle=sim_settings["scene"],
        mm=_cpo.mm,
    )
    # get a subset with receptacles defined
    objects_in_scene = [
        objects_in_scene[i]
        for i in range(len(objects_in_scene))
        if csa.object_has_receptacles(objects_in_scene[i], mm.object_template_manager)
    ]

    def run_cpo_for_obj(obj_handle):
        _cpo.setup_obj_gt(obj_handle)
        _cpo.compute_receptacle_stability(obj_handle, use_gt=True)
        _cpo.compute_receptacle_stability(obj_handle)
        _cpo.compute_receptacle_access_metrics(obj_handle, use_gt=True)
        _cpo.compute_receptacle_access_metrics(obj_handle, use_gt=False)

    # run CPO initialization multi-threaded to unblock viewer initialization and use

    threads = []
    for obj_handle in objects_in_scene:
        run_cpo_for_obj(obj_handle)
        # threads.append(threading.Thread(target=run_cpo_for_obj, args=(obj_handle,)))
    for thread in threads:
        thread.start()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()

    # optional arguments
    parser.add_argument(
        "--scene",
        default="./data/test_assets/scenes/simple_room.glb",
        type=str,
        help='scene/stage file to load (default: "./data/test_assets/scenes/simple_room.glb")',
    )
    parser.add_argument(
        "--dataset",
        default="default",
        type=str,
        metavar="DATASET",
        help='dataset configuration file to use (default: "default")',
    )
    parser.add_argument(
        "--rec-filter-file",
        default="./rec_filter_data.json",
        type=str,
        help='Receptacle filtering metadata (default: "./rec_filter_data.json")',
    )
    parser.add_argument(
        "--init-cpo",
        action="store_true",
        help="Initialize and run the CPO for the current scene.",
    )
    parser.add_argument(
        "--disable-physics",
        action="store_true",
        help="disable physics simulation (default: False)",
    )
    parser.add_argument(
        "--use-default-lighting",
        action="store_true",
        help="Override configured lighting to use default lighting for the stage.",
    )
    parser.add_argument(
        "--hbao",
        action="store_true",
        help="Enable horizon-based ambient occlusion, which provides soft shadows in corners and crevices.",
    )
    parser.add_argument(
        "--enable-batch-renderer",
        action="store_true",
        help="Enable batch rendering mode. The number of concurrent environments is specified with the num-environments parameter.",
    )
    parser.add_argument(
        "--num-environments",
        default=1,
        type=int,
        help="Number of concurrent environments to batch render. Note that only the first environment simulates physics and can be controlled.",
    )
    parser.add_argument(
        "--composite-files",
        type=str,
        nargs="*",
        help="Composite files that the batch renderer will use in-place of simulation assets to improve memory usage and performance. If none is specified, the original scene files will be loaded from disk.",
    )
    parser.add_argument(
        "--width",
        default=1080,
        type=int,
        help="Horizontal resolution of the window.",
    )
    parser.add_argument(
        "--height",
        default=720,
        type=int,
        help="Vertical resolution of the window.",
    )

    args = parser.parse_args()

    if args.num_environments < 1:
        parser.error("num-environments must be a positive non-zero integer.")
    if args.width < 1:
        parser.error("width must be a positive non-zero integer.")
    if args.height < 1:
        parser.error("height must be a positive non-zero integer.")

    # Setting up sim_settings
    sim_settings: Dict[str, Any] = default_sim_settings
    sim_settings["scene"] = args.scene
    sim_settings["scene_dataset_config_file"] = args.dataset
    sim_settings["enable_physics"] = not args.disable_physics
    sim_settings["use_default_lighting"] = args.use_default_lighting
    sim_settings["enable_batch_renderer"] = args.enable_batch_renderer
    sim_settings["num_environments"] = args.num_environments
    sim_settings["composite_files"] = args.composite_files
    sim_settings["window_width"] = args.width
    sim_settings["window_height"] = args.height
    sim_settings["rec_filter_file"] = args.rec_filter_file
    sim_settings["default_agent_navmesh"] = False
    sim_settings["enable_hbao"] = args.hbao

    # don't need auto-navmesh
    sim_settings["default_agent_navmesh"] = False

    mm = habitat_sim.metadata.MetadataMediator()
    mm.active_dataset = sim_settings["scene_dataset_config_file"]

    # initialize the CPO.
    # this will be done in parallel to viewer setup via multithreading
    if args.init_cpo:
        init_cpo_for_scene(sim_settings, mm)

    # start the application
    HabitatSimInteractiveViewer(sim_settings, mm).exec()
