import numpy as np
from chainer import cuda, Variable, function, link, functions, links
from chainer.utils import type_check

class Zoneout(function.Function):
	def __init__(self, zoneout_ratio):
		self.zoneout_ratio = zoneout_ratio

	def check_type_forward(self, in_types):
		type_check.expect(in_types.size() == 1)
		type_check.expect(in_types[0].dtype.kind == 'f')

	def forward(self, x):
		if not hasattr(self, 'mask'):
			xp = cuda.get_array_module(*x)
			if xp == np:
				flag = xp.random.rand(*x[0].shape) >= self.zoneout_ratio
			else:
				flag = (xp.random.rand(*x[0].shape, dtype=np.float32) >= self.zoneout_ratio)
			self.mask = flag
		return x[0] * self.mask,

	def backward(self, x, gy):
		return gy[0] * self.mask,

def zoneout(x, ratio=.5, train=True):
	if train:
		return Zoneout(ratio)(x)
	return x

class QRNN(link.Chain):
	def __init__(self, in_channels, out_channels, kernel_size=2, pooling="f", zoneout=False, zoneout_ratio=0.5):
		self.num_split = len(pooling) + 1
		super(QRNN, self).__init__(W=links.ConvolutionND(1, in_channels, self.num_split * out_channels, kernel_size, stride=1, pad=kernel_size - 1))
		self._in_channels, self._out_channels, self._kernel_size, self._pooling, self._zoneout, self._zoneout_ratio = in_channels, out_channels, kernel_size, pooling, zoneout, zoneout_ratio
		self.reset_state()

	def __call__(self, X, test=False):
		assert isinstance(X, Variable)
		self._test = test
		# remove right paddings
		# e.g.
		# kernel_size = 3
		# pad = 2
		# input sequence with paddings:
		# [0, 0, x1, x2, x3, 0, 0]
		# |< t1 >|
		#     |< t2 >|
		#         |< t3 >|
		pad = self._kernel_size - 1
		WX = self.W(X)[:, :, :-pad]
		return self.pool(functions.split_axis(WX, self.num_split, axis=1))

	def zoneout(self, U):
		if self._zoneout:
			return 1 - zoneout(functions.sigmoid(-U), ratio=self._zoneout_ratio, train=not self._test)
		return functions.sigmoid(U)

	def pool(self, WX):
		# f-pooling
		if len(self._pooling) == 1:
			assert len(WX) == 2
			Z, F = WX
			Z = functions.tanh(Z)
			F = self.zoneout(F)
			for t in xrange(Z.shape[2]):
				zt = Z[:, :, t]
				ft = F[:, :, t]
				if t == 0:
					self.ht = (1 - ft) * zt
					self.H = functions.expand_dims(self.ht, 2)
				else:
					self.ht = ft * self.ht + (1 - ft) * zt
					self.H = functions.concat((self.H, functions.expand_dims(self.ht, 2)), axis=2)
			return self.H

		# fo-pooling
		if len(self._pooling) == 2:
			assert len(WX) == 3
			Z, F, O = WX
			Z = functions.tanh(Z)
			F = self.zoneout(F)
			O = functions.sigmoid(O)
			for t in xrange(Z.shape[2]):
				zt = Z[:, :, t]
				ft = F[:, :, t]
				ot = O[:, :, t]
				if t == 0:
					self.ct = (1 - ft) * zt
				else:
					self.ct = ft * self.ct + (1 - ft) * zt
				self.ht = ot * self.ct
				if t == 0:
					self.H = functions.expand_dims(self.ht, 2)
				else:
					self.H = functions.concat((self.H, functions.expand_dims(self.ht, 2)), axis=2)
			return self.H

		# ifo-pooling
		if len(self._pooling) == 3:
			assert len(WX) == 4
			Z, F, O, I = WX
			Z = functions.tanh(Z)
			F = self.zoneout(F)
			O = functions.sigmoid(O)
			I = functions.sigmoid(I)
			for t in xrange(Z.shape[2]):
				zt = Z[:, :, t]
				ft = F[:, :, t]
				ot = O[:, :, t]
				it = I[:, :, t]
				if t == 0:
					self.ct = (1 - ft) * zt
				else:
					self.ct = ft * self.ct + it * zt
				self.ht = ot * self.ct
				if t == 0:
					self.H = functions.expand_dims(self.ht, 2)
				else:
					self.H = functions.concat((self.H, functions.expand_dims(self.ht, 2)), axis=2)
			return self.H

		raise Exception()

	def reset_state(self):
		self.set_state(None, None, None)

	def set_state(self, ct, ht, H):
		self.ct = ct	# last cell state
		self.ht = ht	# last hidden state
		self.H = H	# all hidden states

	def get_last_hidden_state(self):
		return self.ht

	def get_all_hidden_states(self):
		return self.H

class QRNNEncoder(QRNN):
	pass

class QRNNDecoder(QRNN):
	def __init__(self, in_channels, out_channels, kernel_size=2, pooling="f", zoneout=False, zoneout_ratio=0.5):
		super(QRNNDecoder, self).__init__(in_channels, out_channels, kernel_size, pooling, zoneout, zoneout_ratio)
		self.num_split = len(pooling) + 1
		self.add_link("V", links.Linear(out_channels, self.num_split * out_channels))

	# ht_enc is the last encoder state
	def __call__(self, X, ht_enc, test=False):
		assert isinstance(X, Variable)
		self._test = test
		pad = self._kernel_size - 1
		WX = self.W(X)[:, :, :-pad]
		Vh = self.V(ht_enc)

		# copy Vh
		# e.g.
		# WX = [[[  0	1	2]
		# 		 [	3	4	5]
		# 		 [	6	7	8]
		# Vh = [[11, 12, 13]]
		# 
		# Vh, WX = F.broadcast(F.expand_dims(Vh, axis=2), WX)
		# 
		# WX = [[[  0	1	2]
		# 		 [	3	4	5]
		# 		 [	6	7	8]
		# Vh = [[[ 	11	11	11]
		# 		 [	12	12	12]
		# 		 [	13	13	13]
		Vh, WX = functions.broadcast(functions.expand_dims(Vh, axis=2), WX)
		return self.pool(functions.split_axis(WX + Vh, self.num_split, axis=1))

class QRNNGlobalAttentiveDecoder(QRNNDecoder):
	def __init__(self, in_channels, out_channels, kernel_size=2, zoneout=False, zoneout_ratio=0.5):
		super(QRNNGlobalAttentiveDecoder, self).__init__(in_channels, out_channels, kernel_size, "fo", zoneout, zoneout_ratio)
		self.add_link('o', links.Linear(2 * out_channels, out_channels))

	# X is the input of the decoder
	# ht_enc is the last encoder state
	# H_enc is the encoder's las layer's hidden sates
	def __call__(self, X, ht_enc, H_enc, test=False):
		assert isinstance(X, Variable)
		self._test = test
		pad = self._kernel_size - 1
		WX = self.W(X)[:, :, :-pad]
		Vh = self.V(ht_enc)
		Vh, WX = functions.broadcast(functions.expand_dims(Vh, axis=2), WX)

		# f-pooling
		Z, F, O = functions.split_axis(WX + Vh, 3, axis=1)
		Z = functions.tanh(Z)
		F = self.zoneout(F)
		O = functions.sigmoid(O)
		T = Z.shape[2]

		# compute ungated hidden states
		contexts = []
		for t in xrange(T):
			z = Z[:, :, t]
			f = F[:, :, t]
			if t == 0:
				ct = (1 - f) * z
				contexts.append(ct)
			else:
				ct = f * contexts[-1] + (1 - f) * z
				contexts.append(ct)

		# compute attention weights (eq.8)
		H_enc = functions.swapaxes(H_enc, 1, 2)
		for t in xrange(T):
			ct = contexts[t]
			h = H_enc[:, :t+1, :]
			alpha = functions.batch_matmul(h, ct)
			alpha = functions.softmax(alpha)
			h, alpha = functions.broadcast(h, alpha)	# copy alpha
			kt = functions.sum(alpha * h, axis=1)
			ot = O[:, :, t]
			self.ht = ot * self.o(functions.concat((kt, ct), axis=1))
			if t == 0:
				self.H = functions.expand_dims(self.ht, 2)
			else:
				self.H = functions.concat((self.H, functions.expand_dims(self.ht, 2)), axis=2)
		return self.H