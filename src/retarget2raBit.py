import os 
import sys
import numpy as np 
from tqdm import tqdm

# Modules to load config file and save generated parameters
import json 
import pickle
from easydict import EasyDict as edict 

# DL Modules
import torch
import torch.nn as nn
import torch.optim as optim
import torch.nn.functional as F

torch.backends.cudnn.enabled = True
torch.backends.cudnn.benchmark = True

# Modules
from utils import * # Config details 
from dataloader import OpenCapDataLoader,SMPLLoader # To load TRC file
from meters import Meters # Metrics to measure inverse kinematics
from renderer import Visualizer


import numpy as np
import meshio
import os
import openmesh as om
import texture_util
import shutil


class RaBitModel():
	"""
	RaBit model.
	This model was built by numpy, exclude eyes rebuild.

	"""
	def __init__(self):

		dataroot = os.path.join(RABIT_PATH,"rabit_data/")
		self.mean_file = [dataroot + "shape/mean.obj"]
		self.pca_weight = np.load(dataroot + "shape/pcamat.npy", allow_pickle=True)
		self.clusterdic = np.load(dataroot + "shape/clusterdic.npy", allow_pickle=True).item()

		self.index2cluster = {}
		for key in self.clusterdic.keys():
			val = self.clusterdic[key]
			self.index2cluster[val] = key

		self.joint2index = np.load(dataroot + "shape/joint2index.npy", allow_pickle=True).item()
		joint_order = np.load(dataroot + "shape/pose_order.npy")
		self.weightMatrix = np.load(dataroot + "shape/weight_matrix.npy", allow_pickle=True)

		# reorder joint
		self.ktree_table = np.ones(24) * -1
		ktree_table = np.load(dataroot + "shape/ktree_table.npy", allow_pickle=True).item()
		name2index = {}
		for i in range(1, 24):
			self.ktree_table[i] = ktree_table[i][1]
			name2index[ktree_table[i][0]] = i
		reorder_index = np.zeros(24)
		for i, jointname in enumerate(joint_order):
			if jointname in name2index:
				reorder_index[name2index[jointname]] = i
			else:
				reorder_index[0] = 2
		self.reorder_index = np.array(reorder_index).astype(int)

		# import mesh
		self.points, self.cells = self.mesh_load()

		self.weights = self.weightMatrix
		self.v_template = self.points
		self.shapedirs = self.pca_weight

		self.faces = self.cells
		self._faces = self.faces[0].data
		self.parent = self.ktree_table
		# print(self._faces)

		# INFO:
		# pose_shape: [23, 3]
		# beta_shape: [500]
		self.quads = self._faces.reshape(-1)
		self.pose_shape = [23, 3]
		self.beta_shape = [self.pca_weight.shape[0]]
		self.trans_shape = [3]

		self.pose = np.zeros(self.pose_shape)
		self.beta = np.zeros(self.beta_shape)
		self.trans = np.zeros(self.trans_shape)

		self.verts = None
		self.J = None
		self.R = None

		# update params after model init
		self.update()

	def mesh_load(self):
		"""
		import mesh from file list.

		"""
		mesh_list = []

		for f in self.mean_file:
			try:
				fmesh = meshio.read(f)
				mesh_list.append(fmesh)
			except Exception as e:
				print(e, f)

		points_list = [mesh.points for mesh in mesh_list]
		cells_list = [mesh.cells for mesh in mesh_list]  # faces

		return points_list[0], cells_list[0]

	def set_params(self, pose=None, beta=None, trans=None):
		"""
		Set pose, shape, and/or translation parameters of RaBit model.
		Verices of the model will be updated and returned.
		This version is used for user to apply smpl-like parameter, and additional pose 
		order is applied to keep it consistent as torch of rabit version

		Parameters:
		---------
		pose: Also known as 'theta', a [23, 3] matrix indicating child joint rotation
		relative to parent joint. For root joint it's global orientation.
		Represented in a axis-angle format.

		beta: Parameter for model shape. A vector of shape [500]. Coefficients for
		PCA component. Only 500 components were released by GAP LAB.

		trans: Global translation of shape [3].

		Return:
		------
		Updated vertices.

		"""
		if pose is not None:
			pose = pose[self.reorder_index, :]
			self.pose = pose[1:,:]
		if beta is not None:
			self.beta = beta
		if trans is not None:
			self.trans = trans
		
		self.update()
		return self.verts

	def update(self):
		"""
		Called automatically when parameters are updated
		and numpy implementation.

		"""
		# INFO:
		# shapedirs: (500, 116178)
		# beta: (500,)
		# v_template: (38726, 3)

		# shape blend
		v_shaped = self.shapedirs.T.dot(self.beta) + self.v_template.reshape(-1)

		# rotation matrix for each joint
		# compared to smpl model, rabit needn't simulate the deform of muscle
		pose_cube = np.zeros((24, 1, 3))
		pose_cube[1:, :, :] += self.pose.reshape((-1, 1, 3))
		self.R = self.rodrigues(pose_cube)
		self.v_posed = v_shaped.reshape(-1, 3)

		# generate joints
		self.J = self.joints_list()

		# world transformation of each joint
		G = np.empty((self.ktree_table.shape[0], 4, 4))

		G[0] = self.with_zeros(np.hstack((self.R[0], self.J[0, :].reshape([3, 1]))))
		for i in range(1, self.ktree_table.shape[0]):
			dJ = (self.J[i, :] - self.J[int(self.parent[i]), :]).reshape([3, 1])
			G[i] = G[int(self.parent[i])].dot(
				self.with_zeros(
					np.hstack(
						[self.R[i], dJ]
					)
				)
			)

		# remove the transformation due to the rest pose
		G = G - self.pack(
			np.matmul(
				G,
				np.hstack([self.J, np.zeros([24, 1])]).reshape([24, 4, 1])
			)
		)

		# transformation of each vertex
		T = np.tensordot(self.weights, G, axes=[[1], [0]])
		rest_shape_h = np.hstack((self.v_posed, np.ones([self.v_posed.shape[0], 1])))
		v = np.matmul(T, rest_shape_h.reshape([-1, 4, 1])).reshape([-1, 4])[:, :3]
		self.verts = v + self.trans.reshape([1, 3])

	def rodrigues(self, r):
		"""
		Rodrigues' rotation formula that turns axis-angle vector into rotation
		matrix in a batch-ed manner.

		Parameter:
		----------
		r: Axis-angle rotation vector of shape [batch_size, 1, 3].

		Return:
		-------
		Rotation matrix of shape [batch_size, 3, 3].

		"""
		theta = np.linalg.norm(r, axis=(1, 2), keepdims=True)

		theta = np.maximum(theta, np.finfo(np.float64).tiny)  # avoid zero divide
		r_hat = r / theta
		cos = np.cos(theta)
		z_stick = np.zeros(theta.shape[0])
		m = np.dstack([
			z_stick, -r_hat[:, 0, 2], r_hat[:, 0, 1],
			r_hat[:, 0, 2], z_stick, -r_hat[:, 0, 0],
			-r_hat[:, 0, 1], r_hat[:, 0, 0], z_stick]
		).reshape([-1, 3, 3])
		i_cube = np.broadcast_to(
			np.expand_dims(np.eye(3), axis=0),
			[theta.shape[0], 3, 3]
		)
		A = np.transpose(r_hat, axes=[0, 2, 1])
		B = r_hat
		dot = np.matmul(A, B)
		R = cos * i_cube + (1 - cos) * dot + np.sin(theta) * m
		return R

	def with_zeros(self, x):
		"""
		Append a [0, 0, 0, 1] vector to a [3, 4] matrix.

		Parameter:
		---------
		x: Matrix to be appended.

		Return:
		------
		Matrix after appending of shape [4,4]

		"""
		return np.vstack((x, np.array([[0.0, 0.0, 0.0, 1.0]])))

	def pack(self, x):
		"""
		Append zero matrices of shape [4, 3] to vectors of [4, 1] shape in a batched
		manner.

		Parameter:
		----------
		x: Matrices to be appended of shape [batch_size, 4, 1]

		Return:
		------
		Matrix of shape [batch_size, 4, 4] after appending.

		"""
		return np.dstack((np.zeros((x.shape[0], 4, 3)), x))

	def joints_list(self):
		"""
		generate joints of rabit model based on the mid of maximum & minimun.
		vertices was devided into 25 clusters.

		"""
		J = []
		for i in range(len(self.index2cluster)):
			key = self.index2cluster[i]
			if key == 'RootNode':
				J.append(np.zeros((1, 3)))
				continue
			index_val = self.v_posed[self.joint2index[key], :]
			maxval = index_val.max(axis=0, keepdims=True)
			minval = index_val.min(axis=0, keepdims=True)
			J.append((maxval + minval) / 2)
		J = np.concatenate(J)
		return J

	def save_to_obj_with_texture(self, path):
		"""
		Save the RaBit model into .obj file.

		Parameter:
		---------
		path: Path to save.

		"""
		shutil.copyfile("rabit_data/UV/m_t.mtl", path.replace(".obj", ".mtl"))
		vertex_texture_lines = []
		face_lines = []
		template_vertex_count = 0
		usemtl = ""
		mtllib = ""
		with open("./rabit_data/UV/tri.obj") as file_in_template:
			for line in file_in_template.readlines():
				line = line.replace('\n', '')
				if line.startswith('#'):
					continue
				values = line.split()
				if len(values) == 0:
					continue
				elif values[0] == "usemtl":
					# file_out.write(line + "\n")
					usemtl = line + "\n"
				elif values[0] == "mtllib":
					# file_out.write(line + "\n")
					mtllib = line + "\n"
				elif values[0] == "v":
					template_vertex_count += 1
				elif values[0] == "vt":
					# file_out.write(line + "\n")
					vertex_texture_lines.append(line + "\n")
				elif values[0] == "vn":
					# file_out.write(line + "\n")
					# vertex_normal_lines.append(line + "\n")
					pass
				elif values[0] == "f" or values[0] == "g":
					# file_out.write(line + "\n")
					face_lines.append(line + "\n")
				else:
					pass
		vertex_lines = []
		for v in self.verts:
			vertex_lines.append("v %s %s %s\n" % (str(v[0]), str(v[1]), str(v[2])))
		with open(path, 'w') as file_out:
			file_out.write(mtllib)
			file_out.write(usemtl)
			for v in vertex_lines:
				file_out.write(v)
			for vt in vertex_texture_lines:
				file_out.write(vt)  # template
			for f in face_lines:
				items = f.replace("\n", "").split(" ")
				new_line = ""
				for item in items:
					if item == "f":
						new_line += item
					elif 'g' in item or "G" in item:
						continue
					else:
						splits = item.split("/")
						new_line += " " + splits[0] + "/" + splits[1]
				new_line += "\n"
				file_out.write(new_line)

	def load_smpl_params(self,smpl_params): 

		theta = smpl_params["pose_params"].transpose((1,0,2))
		beta = np.random.rand(*(500,)) * 10 - 5
		# beta[10:] = 0
		beta[10:] = smpl_params["shape_params"]
		# trans = np.zeros(self.trans_shape)
		trans = smpl_params["trans"]

		self.update()

		# rabit.set_params(beta=beta, pose=theta, trans=trans)
		# rabit.save_to_obj_with_texture(save_path)



def retarget_smpl2rabit(sample:OpenCapDataLoader):

	if not os.path.isfile(os.path.join(SMPL_PATH,sample.name+'.pkl')): 
		from retarget2smpl import retarget_opencap2smpl
		smpl_params = retarget_opencap2smpl(sample)
		sample.smpl = smpl_params

	# Log progress
	logger, writer = get_logger(task_name='Retarget2Rabit')
	logger.info(f"Retargetting file:{sample.openCapID}_{sample.label}")

	# Metrics to measure
	meters = Meters()

	# Visualizer
	vis = Visualizer()

	# GPU mode
	if cuda and torch.cuda.is_available():
		device = torch.device('cuda')
	else:
		device = torch.device('cpu')

	# Load SMPL data 
	sample.smpl = smplRetargetter
		
	# Define Rabit Module
	rabit = RaBitModel()	
	rabit.load_smpl_params(sample.smpl)

	# Create random texture and save
	if os.path.isfile(os.path.join(RENDER_PATH,"RaBit",f"{sample.name}.png")):
		os.makedirs(os.path.join(RENDER_PATH,"RaBit"),exist_ok=True)
		texture_util.generate_texture(os.path.join(RENDER_PATH,"RaBit",f"{sample.name}.png"))

	# rabit.save_to_obj_with_texture(save_path)

	sample.rabit = rabit

	vis.render_rabit(sample)



	# smplRetargetter = SMPLRetarget(sample.joints_np.shape[0],device=device).to(device)
	logger.info(f"SMPL to RaBit Retargetting details:{smplRetargetter.index}")	
	logger.info(smplRetargetter.cfg.TRAIN)

	# smplRetargetter.show(target,verts,Jtr,Jtr_offset)
	if not os.path.isdir(SMPL_PATH):
		os.makedirs(SMPL_PATH,exist_ok=True)

	save_path = os.path.join(SMPL_PATH,sample.name+'.pkl')
	logger.info(f'Saving results at:{save_path}')
	smplRetargetter.save(save_path)	


	# Plot HIP and angle joints to visualize 
	for i in range(smplRetargetter.smpl_params['pose_params'].shape[0]):
		# LHIP 
		writer.add_scalar(f"LHip-Z", float(smplRetargetter.smpl_params['pose_params'][i,1*3 + 0]),i )
		writer.add_scalar(f"LHip-Y", float(smplRetargetter.smpl_params['pose_params'][i,1*3 + 1]),i )
		writer.add_scalar(f"LHip-X", float(smplRetargetter.smpl_params['pose_params'][i,1*3 + 2]),i )
		# RHIP 
		writer.add_scalar(f"RHip-Z", float(smplRetargetter.smpl_params['pose_params'][i,2*3 + 0]),i )
		writer.add_scalar(f"RHip-Y", float(smplRetargetter.smpl_params['pose_params'][i,2*3 + 1]),i )
		writer.add_scalar(f"RHip-X", float(smplRetargetter.smpl_params['pose_params'][i,2*3 + 2]),i )

		# L-Ankle 
		writer.add_scalar(f"LAnkle-Z", float(smplRetargetter.smpl_params['pose_params'][i,7*3 + 0]),i )
		writer.add_scalar(f"LAnkle-Y", float(smplRetargetter.smpl_params['pose_params'][i,7*3 + 1]),i )
		writer.add_scalar(f"LAnkle-X", float(smplRetargetter.smpl_params['pose_params'][i,7*3 + 2]),i )

		writer.add_scalar(f"RAnkle-Z", float(smplRetargetter.smpl_params['pose_params'][i,8*3 + 0]),i )
		writer.add_scalar(f"RAnkle-Y", float(smplRetargetter.smpl_params['pose_params'][i,8*3 + 1]),i )
		writer.add_scalar(f"RAnkle-X", float(smplRetargetter.smpl_params['pose_params'][i,8*3 + 2]),i )

	video_dir = os.path.join(RENDER_PATH,f"{sample.openCapID}_{sample.label}_{sample.mcs}")

	if RENDER:
		vis.render_smpl(sample,smplRetargetter,video_dir=video_dir)        


	logger.info('Train ended, min_loss = {:.4f}'.format(
		float(meters.min_loss)))

	writer.flush()
	writer.close()	


	return smplRetargetter.smpl_params



# Load file and render skeleton for each video
def retarget_dataset():
	for subject in os.listdir(DATASET_PATH):
		for sample_path in os.listdir(os.path.join(DATASET_PATH,subject,'MarkerData')):
			sample_path = os.path.join(DATASET_PATH,subject,'MarkerData',sample_path)
			sample = OpenCapDataLoader(sample_path)

			raBit_params = retarget_smpl2rabit(sample)



if __name__ == "__main__": 

	if len(sys.argv) == 1: 
		retarget_dataset()
	else:
		sample_path = sys.argv[1]
		sample = OpenCapDataLoader(sample_path)
		raBit_params = retarget_smpl2rabit(sample)		