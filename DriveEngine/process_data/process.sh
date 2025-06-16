#!/bin/bash
#SBATCH --job-name=uniplan     # 任务名称
#SBATCH --output=/home/lfeng/task_logs/%j.log  # 输出文件
#SBATCH --partition=h100
#SBATCH --qos=vita  
#SBATCH --ntasks-per-node=1      # 只运行一个任务
#SBATCH --cpus-per-task=32
#SBATCH --gpus=1
#SBATCH --time=4:00:00                
#SBATCH --mem=180G 

#rm -rf /work/vita/datasets/nuplan_root/dataset/nuplan-v1.1/rendered_sensor_blobs


module load gcc cuda
export OPENBLAS_NUM_THREADS=1

# Please install https://github.com/autonomousvision/navsim.
# This is used for generating High-Level Driving Commands, used in E2E AD.
export NAVSIM_DEVKIT_ROOT=/work/vita/lanfeng/navsim
export PYTHONPATH=${NAVSIM_DEVKIT_ROOT}:${PYTHONPATH}

split=trainval
# Please download all the nuplan data from https://www.nuscenes.org/nuplan.
export NUPLAN_PATH=/work/vita/datasets/nuplan_root/dataset/nuplan-v1.1
export NUPLAN_DB_PATH=${NUPLAN_PATH}/splits/${split}
export NUPLAN_SENSOR_PATH=/work/vita/datasets/nuplan_root/dataset/sensor_blobs/trainval
export NUPLAN_MAP_VERSION=nuplan-maps-v1.0
export NUPLAN_MAPS_ROOT=/work/vita/datasets/nuplan_root/dataset/maps

OUT_DIR=/work/vita/datasets/nuplan_root/dataset/navsim_logs/${split}

srun python -u create_openscene_metadata.py \
  --nuplan-root-path ${NUPLAN_PATH} \
  --nuplan-db-path ${NUPLAN_DB_PATH} \
  --nuplan-sensor-path ${NUPLAN_SENSOR_PATH} \
  --nuplan-map-version ${NUPLAN_MAP_VERSION} \
  --nuplan-map-root ${NUPLAN_MAPS_ROOT} \
  --out-dir ${OUT_DIR} \
  --split ${split} \
  --thread-num 32 \
  --start-index 0 \
  --end-index 14561
