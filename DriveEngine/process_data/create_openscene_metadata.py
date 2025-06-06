import argparse
import shutil
from typing import Dict, List

# import mmcv
import numpy as np
from os import listdir
from os.path import isfile, join

from pyquaternion import Quaternion
import time
import cv2

from tqdm import tqdm

import os

import multiprocessing
import pickle
from nuplan.common.actor_state.state_representation import StateSE2
from nuplan.common.maps.nuplan_map.map_factory import get_maps_api
from scenarionet.converter.nuplan.type import get_traffic_obj_type, NuPlanEgoType, set_light_status
from scenarionet.converter.utils import nuplan_to_metadrive_vector, compute_angular_velocity
from metadrive.scenario import ScenarioDescription as SD
from metadrive.type import MetaDriveType
from nuplan.database.nuplan_db_orm.nuplandb import NuPlanDB
from nuplan.database.nuplan_db_orm.lidar import Lidar
from nuplan.database.nuplan_db.nuplan_scenario_queries import (
    get_traffic_light_status_for_lidarpc_token_from_db
)
from nuplan.common.maps.maps_datatypes import SemanticMapLayer, StopLineType
from shapely.geometry import Point as Point2D
from shapely.ops import unary_union

from helpers.multiprocess_helper import get_scenes_per_thread
from helpers.canbus import CanBus
from helpers.driving_command import get_driving_command
from helpers.nuplan_cameras_utils import (
    get_log_cam_info, get_closest_start_idx, get_cam_info_from_lidar_pc
)
from helpers.renderer import ScenarioRenderer,save_as_video
from shapely.geometry.linestring import LineString
from shapely.geometry.multilinestring import MultiLineString
import geopandas as gpd

NUPLAN_MAPS_ROOT = os.environ["NUPLAN_MAPS_ROOT"]
filtered_classes = ["traffic_cone", "barrier", "czone_sign", "generic_object"]

def extract_centerline(map_obj, nuplan_center):
    path = map_obj.baseline_path.discrete_path
    points = np.array([nuplan_to_metadrive_vector([pose.x, pose.y], nuplan_center) for pose in path])
    return points

def get_points_from_boundary(boundary, center):
    path = boundary.discrete_path
    points = [(pose.x, pose.y) for pose in path]
    points = nuplan_to_metadrive_vector(points, center)
    return points

def set_light_position(map_api, lane_id, center, target_position=8):
    lane = map_api.get_map_object(str(lane_id), SemanticMapLayer.LANE_CONNECTOR)
    assert lane is not None, "Can not find lane: {}".format(lane_id)
    path = lane.baseline_path.discrete_path
    acc_length = 0
    point = [path[0].x, path[0].y]
    for k, point in enumerate(path[1:], start=1):
        previous_p = path[k - 1]
        acc_length += np.linalg.norm([point.x - previous_p.x, point.y - previous_p.y])
        if acc_length > target_position:
            break
    return [point.x - center[0], point.y - center[1]]

def extract_map_features(map_api, center, radius=200):
    ret = {}
    np.seterr(all='ignore')
    # Center is Important !
    layer_names = [
        SemanticMapLayer.LANE_CONNECTOR,
        SemanticMapLayer.LANE,
        SemanticMapLayer.CROSSWALK,
        SemanticMapLayer.INTERSECTION,
        SemanticMapLayer.STOP_LINE,
        SemanticMapLayer.WALKWAYS,
        SemanticMapLayer.CARPARK_AREA,
        SemanticMapLayer.ROADBLOCK,
        SemanticMapLayer.ROADBLOCK_CONNECTOR,

        # unsupported yet
        # SemanticMapLayer.STOP_SIGN,
        # SemanticMapLayer.DRIVABLE_AREA,
    ]
    center_for_query = Point2D(*center)
    nearest_vector_map = map_api.get_proximal_map_objects(center_for_query, radius, layer_names)
    #boundaries = map_api._get_vector_map_layer(SemanticMapLayer.BOUNDARIES)
    # Filter out stop polygons in turn stop
    if SemanticMapLayer.STOP_LINE in nearest_vector_map:
        stop_polygons = nearest_vector_map[SemanticMapLayer.STOP_LINE]
        nearest_vector_map[SemanticMapLayer.STOP_LINE] = [
            stop_polygon for stop_polygon in stop_polygons if stop_polygon.stop_line_type != StopLineType.TURN_STOP
        ]
    block_polygons = []
    for layer in [SemanticMapLayer.ROADBLOCK, SemanticMapLayer.ROADBLOCK_CONNECTOR]:
        for block in nearest_vector_map[layer]:
            edges = sorted(block.interior_edges, key=lambda lane: lane.index) \
                if layer == SemanticMapLayer.ROADBLOCK else block.interior_edges
            for index, lane_meta_data in enumerate(edges):
                if not hasattr(lane_meta_data, "baseline_path"):
                    continue
                if isinstance(lane_meta_data.polygon.boundary, MultiLineString):
                    boundary = gpd.GeoSeries(lane_meta_data.polygon.boundary).explode(index_parts=True)
                    sizes = []
                    for idx, polygon in enumerate(boundary[0]):
                        sizes.append(len(polygon.xy[1]))
                    points = boundary[0][np.argmax(sizes)].xy
                elif isinstance(lane_meta_data.polygon.boundary, LineString):
                    points = lane_meta_data.polygon.boundary.xy
                polygon = [[points[0][i], points[1][i]] for i in range(len(points[0]))]
                polygon = nuplan_to_metadrive_vector(polygon, nuplan_center=[center[0], center[1]])

                # According to the map attributes, lanes are numbered left to right with smaller indices being on the
                # left and larger indices being on the right.
                # @ See NuPlanLane.adjacent_edges()
                ret[lane_meta_data.id] = {
                    SD.TYPE: MetaDriveType.LANE_SURFACE_STREET \
                        if layer == SemanticMapLayer.ROADBLOCK else MetaDriveType.LANE_SURFACE_UNSTRUCTURE,
                    SD.POLYLINE: extract_centerline(lane_meta_data, center),
                    SD.ENTRY: [edge.id for edge in lane_meta_data.incoming_edges],
                    SD.EXIT: [edge.id for edge in lane_meta_data.outgoing_edges],
                    SD.LEFT_NEIGHBORS: [edge.id for edge in block.interior_edges[:index]] \
                        if layer == SemanticMapLayer.ROADBLOCK else [],
                    SD.RIGHT_NEIGHBORS: [edge.id for edge in block.interior_edges[index + 1:]] \
                        if layer == SemanticMapLayer.ROADBLOCK else [],
                    SD.POLYGON: polygon
                }
                if layer == SemanticMapLayer.ROADBLOCK_CONNECTOR:
                    continue
                left = lane_meta_data.left_boundary
                if left.id not in ret:
                    # only broken line in nuPlan data
                    # line_type = get_line_type(int(boundaries.loc[[str(left.id)]]["boundary_type_fid"]))
                    line_type = MetaDriveType.LINE_BROKEN_SINGLE_WHITE
                    if line_type != MetaDriveType.LINE_UNKNOWN:
                        ret[left.id] = {SD.TYPE: line_type, SD.POLYLINE: get_points_from_boundary(left, center)}

            if layer == SemanticMapLayer.ROADBLOCK:
                block_polygons.append(block.polygon)

    # walkway
    for area in nearest_vector_map[SemanticMapLayer.WALKWAYS]:
        if isinstance(area.polygon.exterior, MultiLineString):
            boundary = gpd.GeoSeries(area.polygon.exterior).explode(index_parts=True)
            sizes = []
            for idx, polygon in enumerate(boundary[0]):
                sizes.append(len(polygon.xy[1]))
            points = boundary[0][np.argmax(sizes)].xy
        elif isinstance(area.polygon.exterior, LineString):
            points = area.polygon.exterior.xy
        polygon = [[points[0][i], points[1][i]] for i in range(len(points[0]))]
        polygon = nuplan_to_metadrive_vector(polygon, nuplan_center=[center[0], center[1]])
        ret[area.id] = {
            SD.TYPE: MetaDriveType.BOUNDARY_SIDEWALK,
            SD.POLYGON: polygon,
        }

    # corsswalk
    for area in nearest_vector_map[SemanticMapLayer.CROSSWALK]:
        if isinstance(area.polygon.exterior, MultiLineString):
            boundary = gpd.GeoSeries(area.polygon.exterior).explode(index_parts=True)
            sizes = []
            for idx, polygon in enumerate(boundary[0]):
                sizes.append(len(polygon.xy[1]))
            points = boundary[0][np.argmax(sizes)].xy
        elif isinstance(area.polygon.exterior, LineString):
            points = area.polygon.exterior.xy
        polygon = [[points[0][i], points[1][i]] for i in range(len(points[0]))]
        polygon = nuplan_to_metadrive_vector(polygon, nuplan_center=[center[0], center[1]])
        ret[area.id] = {
            SD.TYPE: MetaDriveType.CROSSWALK,
            SD.POLYGON: polygon,
        }

    interpolygons = [block.polygon for block in nearest_vector_map[SemanticMapLayer.INTERSECTION]]
    boundaries = gpd.GeoSeries(unary_union(interpolygons + block_polygons)).boundary.explode(index_parts=True)
    # boundaries.plot()
    # plt.show()
    for idx, boundary in enumerate(boundaries[0]):
        block_points = np.array(list(i for i in zip(boundary.coords.xy[0], boundary.coords.xy[1])))
        block_points = nuplan_to_metadrive_vector(block_points, center)
        id = "boundary_{}".format(idx)
        ret[id] = {SD.TYPE: MetaDriveType.LINE_SOLID_SINGLE_WHITE, SD.POLYLINE: block_points}
    np.seterr(all='warn')
    return ret


def create_nuplan_info(args):
    render_sensor_path = args.nuplan_sensor_path.replace("sensor_blobs", "rendered_sensor_blobs")
    os.makedirs(render_sensor_path, exist_ok=True)

    nuplan_sensor_root = args.nuplan_sensor_path
    # get all db files & assign db files for current thread.
    log_sensors = os.listdir(nuplan_sensor_root)
    nuplan_db_path = args.nuplan_db_path
    db_names_with_extension = [
        f for f in listdir(nuplan_db_path) if isfile(join(nuplan_db_path, f))]
    db_names = [name[:-3] for name in db_names_with_extension]
    db_names.sort()
    db_names_splited, start = get_scenes_per_thread(db_names, args.thread_num)
    log_idx = start

    renderer = ScenarioRenderer()
    # For each sequence...
    for log_db_name in db_names_splited:

        frame_infos = []
        scene_list = []
        broken_frame_tokens = []

        log_db = NuPlanDB(args.nuplan_root_path, join(nuplan_db_path, log_db_name + ".db"), None)
        log_name = log_db.log_name
        log_token = log_db.log.token
        map_location = log_db.log.map_version
        vehicle_name = log_db.log.vehicle_name

        map_api = get_maps_api(NUPLAN_MAPS_ROOT, "nuplan-maps-v1.0", map_location)  # NOTE: lru cached

        log_file = os.path.join(nuplan_db_path, log_db_name + ".db")


        frame_idx = 0
        log_idx += 1

        # list (sequence) of point clouds (each frame).
        lidar_pc_list = log_db.lidar_pc
        lidar_pcs = lidar_pc_list

        log_cam_infos = get_log_cam_info(log_db.log)
        start_idx = get_closest_start_idx(log_db.log, lidar_pcs)

        # Find key_frames (controled by args.sample_interval)
        lidar_pc_list = lidar_pc_list[start_idx :: args.sample_interval]
        index = -1
        time_step = 0
        img_list = []
        for lidar_pc in tqdm(lidar_pc_list, dynamic_ncols=True):
            index += 1
            # LiDAR attributes.
            lidar_pc_token = lidar_pc.token
            scene_token = lidar_pc.scene_token
            pc_file_name = lidar_pc.filename
            next_token = lidar_pc.next_token
            prev_token = lidar_pc.prev_token
            lidar_token = lidar_pc.lidar_token
            time_stamp = lidar_pc.timestamp
            scene_name = f"log-{log_idx:04d}-{lidar_pc.scene.name}"
            lidar_boxes = lidar_pc.lidar_boxes
            roadblock_ids = [
                str(roadblock_id)
                for roadblock_id in str(lidar_pc.scene.roadblock_ids).split(" ")
                if len(roadblock_id) > 0
            ]

            if scene_token not in scene_list:
                scene_list.append(scene_token)
                frame_idx = 0

            can_bus = CanBus(lidar_pc).tensor
            lidar = log_db.session.query(Lidar).filter(Lidar.token == lidar_token).all()
            #pc_file_path = os.path.join(args.nuplan_sensor_path, pc_file_name)
            # if not os.path.exists(pc_file_path):  # some lidar files are missing.
            #     broken_frame_tokens.append(lidar_pc_token)
            #     frame_str = f"{log_db_name}, {lidar_pc_token}"
            #     tqdm.write(f"missing lidar files: {frame_str}")
            #     continue

            traffic_lights = []
            for traffic_light_status in get_traffic_light_status_for_lidarpc_token_from_db(
                log_file, lidar_pc_token
            ):
                lane_connector_id: int = traffic_light_status.lane_connector_id
                is_red: bool = traffic_light_status.status.value == 2
                traffic_light_position = set_light_position(map_api, lane_connector_id, [lidar_pc.ego_pose.x, lidar_pc.ego_pose.y])
                traffic_lights.append((lane_connector_id, is_red, traffic_light_position))

            ego_pose = StateSE2(
                lidar_pc.ego_pose.x,
                lidar_pc.ego_pose.y,
                lidar_pc.ego_pose.quaternion.yaw_pitch_roll[0],
            )
            driving_command = get_driving_command(ego_pose, map_api, roadblock_ids)

            scenario = {}
            map_features = extract_map_features(map_api, [lidar_pc.ego_pose.x, lidar_pc.ego_pose.y], radius=200)
            scenario['map_features'] = map_features
            scenario['ego_pos'] = [lidar_pc.ego_pose.x, lidar_pc.ego_pose.y]
            scenario['ego_heading'] = lidar_pc.ego_pose.quaternion.yaw_pitch_roll[0]
            scenario['traffic_lights'] = traffic_lights
            
            info = {
                "token": lidar_pc_token,
                "frame_idx": frame_idx,
                "timestamp": time_stamp,
                "log_name": log_name,
                "log_token": log_token,
                "scene_name": scene_name,
                "scene_token": scene_token,
                "map_location": map_location,
                "roadblock_ids": roadblock_ids,
                "vehicle_name": vehicle_name,
                "can_bus": can_bus,
                "lidar_path": pc_file_name,  # use the relative path.
                "lidar2ego_translation": lidar[0].translation_np,
                "lidar2ego_rotation": [
                    lidar[0].rotation.w,
                    lidar[0].rotation.x,
                    lidar[0].rotation.y,
                    lidar[0].rotation.z,
                ],
                "ego2global_translation": can_bus[:3],
                "ego2global_rotation": can_bus[3:7],
                "ego_dynamic_state": [
                    lidar_pc.ego_pose.vx,
                    lidar_pc.ego_pose.vy,
                    lidar_pc.ego_pose.acceleration_x,
                    lidar_pc.ego_pose.acceleration_y,
                ],
                "traffic_lights": traffic_lights,
                "driving_command": driving_command, 
                "cams": dict(),
            }
            info["sample_prev"] = None
            info["sample_next"] = None

            if index > 0:  # find prev.
                info["sample_prev"] = lidar_pc_list[index - 1].token
            if index < len(lidar_pc_list) - 1:  # find next.
                next_key_token = lidar_pc_list[index + 1].token
                next_key_scene = lidar_pc_list[index + 1].scene_token
                info["sample_next"] = next_key_token
            else:
                next_key_token, next_key_scene = None, None

            if next_key_token == None or next_key_token == "":
                frame_idx = 0
            else:
                if next_key_scene != scene_token:
                    frame_idx = 0
                else:
                    frame_idx += 1

            # Parse lidar2ego translation.
            l2e_r = info["lidar2ego_rotation"]
            l2e_t = info["lidar2ego_translation"]
            e2g_r = info["ego2global_rotation"]
            e2g_t = info["ego2global_translation"]
            l2e_r_mat = Quaternion(l2e_r).rotation_matrix
            e2g_r_mat = Quaternion(e2g_r).rotation_matrix

            # add lidar2global: map point coord in lidar to point coord in the global
            l2e = np.eye(4)
            l2e[:3, :3] = l2e_r_mat
            l2e[:3, -1] = l2e_t
            e2g = np.eye(4)
            e2g[:3, :3] = e2g_r_mat
            e2g[:3, -1] = e2g_t
            lidar2global = np.dot(e2g, l2e)
            info["ego2global"] = e2g
            info["lidar2ego"] = l2e
            info["lidar2global"] = lidar2global

            # obtain 8 image's information per frame
            cams, camera_exists = get_cam_info_from_lidar_pc(log_db.log, lidar_pc, log_cam_infos)
            info["camera_exists"] = camera_exists

            # if not camera_exists:
            #     broken_frame_tokens.append(lidar_pc_token)
            #     #frame_str = f"{log_db_name}, {lidar_pc_token}"
            #     #tqdm.write(f"not all cameras are available: {frame_str}")
            #     #continue
            info["cams"] = cams

            # Parse 3D object labels.
            if not args.is_test:
                if args.filter_instance:
                    fg_lidar_boxes = [
                        box for box in lidar_boxes if box.category.name not in filtered_classes
                    ]
                else:
                    fg_lidar_boxes = lidar_boxes

                instance_tokens = [item.token for item in fg_lidar_boxes]
                track_tokens = [item.track_token for item in fg_lidar_boxes]

                inv_ego_r = lidar_pc.ego_pose.trans_matrix_inv
                ego_yaw = lidar_pc.ego_pose.quaternion.yaw_pitch_roll[0]

                locs = np.array(
                    [
                        np.dot(
                            inv_ego_r[:3, :3],
                            (b.translation_np - lidar_pc.ego_pose.translation_np).T,
                        ).T
                        for b in fg_lidar_boxes
                    ]
                ).reshape(-1, 3)
                dims = np.array([[b.length, b.width, b.height] for b in fg_lidar_boxes]).reshape(
                    -1, 3
                )
                rots = np.array([b.yaw for b in fg_lidar_boxes]).reshape(-1, 1)
                rots = rots - ego_yaw

                velocity_3d = np.array([[b.vx, b.vy, b.vz] for b in fg_lidar_boxes]).reshape(-1, 3)
                for i in range(len(fg_lidar_boxes)):
                    velo = velocity_3d[i]
                    velo = velo @ np.linalg.inv(e2g_r_mat).T @ np.linalg.inv(l2e_r_mat).T
                    velocity_3d[i] = velo

                names = [box.category.name for box in fg_lidar_boxes]
                names = np.array(names)
                gt_boxes_nuplan = np.concatenate([locs, dims, rots], axis=1)
                locs_world = np.array(
                    [
                        b.translation_np - lidar_pc.ego_pose.translation_np
                        for b in fg_lidar_boxes
                    ]
                ).reshape(-1, 3)
                gt_boxes_world = np.concatenate([locs_world, dims, rots+ego_yaw], axis=1)
                    
                info["anns"] = dict(
                    gt_boxes=gt_boxes_nuplan,
                    gt_boxes_world=gt_boxes_world,
                    gt_names=names,
                    gt_velocity_3d=velocity_3d.reshape(-1, 3),
                    instance_tokens=instance_tokens,
                    track_tokens=track_tokens,
                )
                scenario['anns'] = info['anns']

                rendered_cameras = renderer.observe(scenario)
                img_list.append(rendered_cameras)
                if len(img_list) == 200:
                    save_as_video(img_list, "test.mp4")
                    break
                # add rendered images to info
                for cam_id, rendered in rendered_cameras.items():
                    real_path = cams[cam_id]['data_path']
                    full_rendered_path = os.path.join(render_sensor_path, real_path)
                    os.makedirs(os.path.dirname(full_rendered_path), exist_ok=True)
                    cv2.imwrite(full_rendered_path, rendered[:, :, ::-1])
            frame_infos.append(info)

        del map_api

        # # after check.
        # for info in frame_infos:
        #     if info["sample_prev"] in broken_frame_tokens:
        #         info["sample_prev"] = None
        #     if info["sample_next"] in broken_frame_tokens:
        #         info["sample_next"] = None

        pkl_file_path = f"{args.out_dir}/{log_name}.pkl"
        os.makedirs(args.out_dir, exist_ok=True)

        with open(pkl_file_path, "wb") as f:
            pickle.dump(frame_infos, f, protocol=pickle.HIGHEST_PROTOCOL)


def parse_args():
    parser = argparse.ArgumentParser(description="Train a detector")
    parser.add_argument(
        "--thread-num", type=int, default=16, help="number of threads for multi-processing."
    )

    # directory configurations.
    parser.add_argument("--nuplan-root-path", help="the path to nuplan root path.")
    parser.add_argument("--nuplan-db-path", help="the dir saving nuplan db.")
    parser.add_argument("--nuplan-sensor-path", help="the dir to nuplan sensor data.")
    parser.add_argument("--nuplan-map-version", help="nuplan mapping dataset version.")
    parser.add_argument("--nuplan-map-root", help="path to nuplan map data.")
    parser.add_argument("--out-dir", help="output path.")

    parser.add_argument(
        "--sample-interval", type=int, default=10, help="interval of key frame samples."
    )

    # split.
    parser.add_argument("--is-test", action="store_true", help="Dealing with Test set data.")
    parser.add_argument(
        "--filter-instance", action="store_true", help="Ignore instances in filtered_classes."
    )
    parser.add_argument("--split", type=str, default="train", help="Train/Val/Test set.")

    args = parser.parse_args()
    return args


if __name__ == "__main__":
    args = parse_args()

    nuplan_root_path = args.nuplan_root_path
    nuplan_db_path = args.nuplan_db_path
    nuplan_sensor_path = args.nuplan_sensor_path
    nuplan_map_version = args.nuplan_map_version
    nuplan_map_root = args.nuplan_map_root
    out_dir = args.out_dir

    manager = multiprocessing.Manager()
    # return_dict = manager.dict()
    threads = []
    for x in range(args.thread_num):
        t = multiprocessing.Process(
            target=create_nuplan_info,
            name=str(x),
            args=(args,),
        )
        threads.append(t)
    for thr in threads:
        thr.start()
    for thr in threads:
        if thr.is_alive():
            thr.join()
